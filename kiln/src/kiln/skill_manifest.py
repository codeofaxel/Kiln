"""Skill manifest generation and distribution for Kiln.

Provides the skill definition, configuration requirements, and tool
catalog so that AI agents can self-discover Kiln's capabilities without
manual SKILL.md file copying.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SkillManifest:
    """Machine-readable skill manifest for agent integration."""

    name: str = "kiln"
    version: str = ""  # populated from package version
    description: str = "3D printer control and monitoring via CLI and MCP"

    # Configuration requirements
    required_env: list[str] = field(
        default_factory=lambda: [
            "KILN_PRINTER_HOST",
            "KILN_PRINTER_API_KEY",
            "KILN_PRINTER_TYPE",
        ]
    )
    optional_env: list[str] = field(
        default_factory=lambda: [
            "KILN_PRINTER_MODEL",
            "KILN_PRINTER_SERIAL",
            "KILN_AUTONOMY_LEVEL",
            "KILN_HEATER_TIMEOUT",
            "KILN_CRAFTCLOUD_API_KEY",
            "GEMINI_API_KEY",
            "KILN_MESHY_API_KEY",
            "KILN_TRIPO3D_API_KEY",
            "KILN_STABILITY_API_KEY",
            "KILN_THIRDOS_API_KEY",
        ]
    )

    # Capabilities
    interfaces: list[str] = field(default_factory=lambda: ["cli", "mcp"])
    tool_count: int = 0  # populated dynamically
    mcp_capability_count: int = 0  # tools + prompts + resources
    safety_levels: list[str] = field(
        default_factory=lambda: [
            "safe",
            "guarded",
            "confirm",
            "emergency",
        ]
    )

    # Setup verification
    setup_command: str = "kiln verify"
    health_command: str = "kiln status --json"

    # Tool discovery — how agents find tools among 400+ MCP entries.
    # `total_tools_note` is computed from the live MCP registry at
    # construction time so the numbers can never drift from reality.
    discovery: dict[str, Any] = field(default_factory=lambda: _build_discovery_section())

    # Tier system — what paid tiers unlock
    tiers: dict[str, Any] = field(
        default_factory=lambda: {
            "free": (
                "Local printing, slicing, basic marketplace search, 1 "
                "printer. Free agents can still discover Pro/Business stubs "
                "via ToolSearch for upgrade messaging."
            ),
            "pro": (
                "Product generators (coasters, keychains, ornaments, etc.), "
                "decoration, procedural textures, cloud sync, design "
                "versioning, manual speed control, print learning. Still 1 "
                "printer — fleet starts at Business. "
                "$49/mo. https://kiln3d.com/pricing"
            ),
            "business": (
                "Everything in Pro + fleet-wide ops (3 printers and 3 team "
                "seats included, metered to caps of 50 and 10), "
                "layer-scheduled speed adjustments, material "
                "compliance tracking, custom safety profiles, webhooks. "
                "$199/mo. https://kiln3d.com/pricing"
            ),
            "enterprise": (
                "Everything in Business + an uncapped fleet (50 printers "
                "included), SSO, RBAC, audit trails, lockable safety "
                "profiles, 99.9% uptime SLA. "
                "Custom-quoted. https://kiln3d.com/enterprise"
            ),
        }
    )

    # Agent behavioral guidance — how to use Kiln well
    agent_rules: list[str] = field(
        default_factory=lambda: [
            "ALWAYS use MCP tools instead of CLI commands (e.g. monitor_print() not 'kiln status').",
            "When monitor_print() returns a snapshot path, READ the image file and show it inline to the user.",
            "ALWAYS display the full monitoring report — never summarize or omit fields like cost estimate.",
            "After generating a model, ALWAYS call preview_generated_model() to render multi-angle previews BEFORE printing.",
            "Check the bottom view in previews for bed adhesion issues (elephant's foot, insufficient contact).",
            "Use design_session(verb=\"start\", idea=\"...\") as the FIRST step for any new design — captures the user's saved goal at the duty / environment / materials / safety layer and drives generation, the audit, and the post-print review.",
            "Saving something reusable has three kinds with different homes: a whole printable object is a design, a reusable mechanical edit or attachable part is a feature, a reusable visual layer (logo / photo / pattern) is a decoration — choose the kind before saving and ask the user if it's ambiguous.",
            "Use recommend_design_material() for material selection and find_design_templates() for proven templates.",
            "Run preflight_check() before every print job.",
            "Never guess on physical operations — ask the user when uncertain.",
            "For print failures: analyze_print_failure_smart() → get_recovery_plan() → retry_print_with_fix().",
            "Use build_generation_prompt() to enhance generation prompts with design intelligence before calling generate_model().",
            "Use ams_status() to check loaded AMS filaments before multi-color prints.",
        ]
    )

    # Common workflows agents should know
    workflows: dict[str, list[str]] = field(
        default_factory=lambda: {
            "create_custom_object_free": [
                "DEFAULT for 'make me a ...': write OpenSCAD yourself, compile with compile_scad — free, no API key, works on every tier",
                "visualize_model(file_path) — show a preview image (MANDATORY, every iteration round)",
                "iterate: edit the OpenSCAD, recompile, show a fresh preview each round; offer 'loop ~3 more then check in, or loop until it's done?'",
                "validate_generated_mesh / analyze_printability — printability check (conservative defaults are free)",
                "Cloud AI providers (the design_and_generate path) are opt-in and need the user's own API key — don't suggest unless asked or the shape is organic/photo-based",
                "From a photo/sketch: SEE it yourself and write OpenSCAD to match — no image-to-3D provider needed; generate_model_from_image (Meshy) is opt-in only",
                "Organic/curvy shapes: use the bundled BOSL2 metaballs/skin in your OpenSCAD; expect a few refine rounds",
            ],
            "design_and_generate": [
                "design_session(verb=\"start\", idea=\"...\") — capture the saved goal",
                "design_session(verb=\"update\", session_id, answers) — answer any follow-up questions",
                "design_session(verb=\"save\", session_id) — lock in the goal",
                "design_session(verb=\"generate\", session_id) — produce the STL (drives audit + outcome tracker)",
                "preview_generated_model(model_id) — multi-angle visual check (MANDATORY)",
                "validate_generated_mesh(model_id) — printability safety check",
                "slice_model(file_path) — slice to gcode",
                "preflight_check() — verify printer ready",
                "start_print(file) — begin printing",
                "monitor_print() — track progress, show snapshots and cost",
            ],
            "monitor_active_print": [
                "monitor_print() — full report with progress, temps, cost, snapshot",
                "Read the snapshot image file and display it inline to user",
                "Show ALL report fields — never omit cost estimate or temps",
            ],
            "multi_color_print": [
                "ams_status() — check what colors are loaded in AMS",
                "For same-object multi-color copies: kiln slice model.stl --copies N --ams-mapping 0,1,2 --print-after",
                "For different-object multi-material: multi_material_print(objects_json, ...)",
                "check_multi_material_pairing() — verify material/color compatibility",
            ],
            "find_and_print": [
                "search_all_models(query) — find models on Thingiverse etc.",
                "download_and_upload(url) — download and send to printer",
                "preflight_check() — verify printer ready",
                "start_print(file) — begin printing",
            ],
            "failure_recovery": [
                "analyze_print_failure_smart(description) — automated root cause analysis",
                "get_recovery_plan(failure_id) — recovery options",
                "retry_print_with_fix(file, fixes) — re-slice with corrections",
                "troubleshoot_print_issue(issue) — design intelligence diagnosis",
            ],
            "design_intelligence": [
                "design_session(verb=\"start\", idea=\"...\") — saved-goal Q&A entry point; drives audit + post-print review",
                "list_design_goals(filter_status=\"all\") — inbox of every goal the user has committed",
                "analyze_design_requirements(requirements) — internal functional-analysis lookup (design_session calls into this; agents rarely call directly)",
                "get_material_design_profile(material) — material-specific design rules",
                "find_design_templates(use_case) — proven design templates (18 templates)",
                "estimate_structural_load(geometry, material) — load capacity analysis",
                "validate_design_for_requirements(design, reqs) — verification",
                "get_post_processing_guide(material) — finishing guidance",
            ],
            "fleet_management": [
                "fleet_status() — all printers overview",
                "route_print_job(file, requirements) — intelligent job routing",
                "fleet_job_status(job_id) — track distributed jobs",
            ],
        }
    )

    # Tools agents should reach for by use case
    tool_recommendations: dict[str, str] = field(
        default_factory=lambda: {
            "printer_status": "printer_status() — current state, temps, progress",
            "monitoring": "monitor_print() — full report with snapshot and cost",
            "tier_diagnostic": "check_my_tier() — answer plan/subscription/paywall questions ('what tier am I on', 'why does it say I need Pro', 'do I have to pay')",
            "saved_goal_start": "design_session(verb=\"start\", idea=\"...\") — start a new saved goal; the Q&A captures duty / environment / materials / safety and drives the audit + post-print review",
            "saved_goal_list":  "list_design_goals(filter_status=\"all\") — list every saved goal; filter by status (needs_questions / ready_to_generate / matches_what_you_asked_for) to triage",
            "design_requirements": "analyze_design_requirements(requirements) — internal functional-requirements lookup (design_session calls into this; agents rarely call directly)",
            "design_templates": "find_design_templates(use_case) — proven templates (18 in library)",
            "material_selection": "recommend_design_material(use_case) — intelligent material pick",
            "material_rules": "get_material_design_profile(material) — constraints and rules",
            "load_analysis": "estimate_structural_load(geometry, material) — strength validation",
            "generation": "generate_model(description) — text-to-3D (Gemini, Meshy, Tripo3D, Stability)",
            "generation_enhance": "build_generation_prompt(brief) — enhance with design intelligence",
            "preview": "preview_generated_model(model_id) — multi-angle visual check (mandatory)",
            "slicing": "slice_model(file_path) — STL/3MF to gcode",
            "adaptive_slicing": "generate_adaptive_slicing_plan(file) — quality/time tradeoff",
            "printing": "start_print(file) — begin a print job",
            "safety": "preflight_check() — pre-print safety verification",
            "ams_colors": "ams_status() — check loaded AMS filaments and colors",
            "multi_material": "multi_material_print(objects_json) — different objects in different materials",
            "failure_analysis": "analyze_print_failure_smart(description) — root cause analysis",
            "recovery": "retry_print_with_fix(file, fixes) — re-slice with corrections",
            "troubleshooting": "troubleshoot_print_issue(issue) — design intelligence diagnosis",
            "post_processing": "get_post_processing_guide(material) — finishing techniques",
            "fleet": "fleet_status() — fleet overview and job routing",
            "cost_estimate": "estimate_cost(file) — print cost estimation",
        }
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (enum-safe)."""
        return asdict(self)


def get_version() -> str:
    """Get the installed Kiln package version."""
    try:
        from importlib.metadata import version

        return version("kiln")
    except Exception:
        return "unknown"


def get_tool_count() -> int:
    """Return the live count of MCP tools registered on the server.

    Primary source: ``kiln.server.mcp._tool_manager.list_tools()`` —
    the authoritative count of every tool callable in this session,
    including kiln-pro plugins and manifest stubs when they're
    loaded. This matches the number reported by ``get_started()``
    so agents see consistent answers across both tools.

    Fallback: ``data/tool_safety.json`` classification count.  Used
    only when the live registry isn't reachable (e.g. during cold
    startup, or in test harnesses that import skill_manifest before
    the MCP server initializes).  Returns 0 if both sources fail.
    """
    # Primary: live MCP registry (single source of truth for "how
    # many tools are actually callable right now").
    try:
        import kiln.server as _srv

        return len(_srv.mcp._tool_manager.list_tools())
    except Exception:  # noqa: BLE001 — any import/attr error → fallback
        pass

    # Fallback: static classification file.
    try:
        data_path = Path(__file__).resolve().parent / "data" / "tool_safety.json"
        if data_path.is_file():
            raw = json.loads(data_path.read_text(encoding="utf-8"))
            return len(raw.get("classifications", {}))
    except (json.JSONDecodeError, OSError, KeyError):
        pass
    return 0


def get_mcp_capability_counts() -> dict[str, int]:
    """Return live MCP counts split by first-class surface type.

    MCP exposes tools, prompts, and resources.  Tools are callable
    functions; prompts and resources are also first-class MCP
    capabilities and count toward the full MCP surface area.
    """
    try:
        import kiln.server as _srv

        tools = len(_srv.mcp._tool_manager.list_tools())
        prompts = len(_srv.mcp._prompt_manager.list_prompts())
        resources = len(_srv.mcp._resource_manager.list_resources())
        return {
            "tools": tools,
            "prompts": prompts,
            "resources": resources,
            "total": tools + prompts + resources,
        }
    except Exception:  # noqa: BLE001 — any registry error → fallback
        tools = get_tool_count()
        return {"tools": tools, "prompts": 0, "resources": 0, "total": tools}


def get_cli_count() -> int:
    """Count total CLI commands — leaves + groups, incl. kiln-pro extensions.

    Walks the Click command tree rooted at ``kiln.cli.main.cli`` and
    returns ``len(leaves) + num_groups``.  This matches the documented
    methodology in ``.dev/LESSONS_LEARNED.md`` and counts everything
    a user can type after ``kiln `` — every subcommand *and* every
    parent group (which is itself invocable as ``kiln <group>``).

    kiln-pro extends the tree at import time via ``register_pro_commands``,
    so counting at call time automatically picks up pro CLI commands
    when kiln-pro is installed.  Returns 0 if the CLI can't be imported
    (e.g. missing click dependency in a minimal environment).
    """
    try:
        import click

        from kiln.cli.main import cli
    except Exception:  # noqa: BLE001 — click or cli import failure
        return 0

    def walk_leaves(group: click.Group, prefix: str = "") -> list[str]:
        out: list[str] = []
        for name, cmd in group.commands.items():
            full = f"{prefix} {name}".strip()
            if isinstance(cmd, click.Group):
                out.extend(walk_leaves(cmd, full))
            else:
                out.append(full)
        return out

    def count_groups(group: click.Group) -> int:
        n = 0
        for _, cmd in group.commands.items():
            if isinstance(cmd, click.Group):
                n += 1 + count_groups(cmd)
        return n

    return len(walk_leaves(cli)) + count_groups(cli)


def _build_discovery_section() -> dict[str, Any]:
    """Build the manifest `discovery` section with live tool counts.

    Called lazily by ``SkillManifest``'s default_factory so every
    generated manifest reflects the currently registered MCP tools
    (public + kiln-pro) rather than a stale hand-edited blurb.
    """
    split = get_tool_counts_split()
    mcp_counts = get_mcp_capability_counts()
    if split["total"]:
        total_tools_note = (
            f"Public Kiln ships {split['public']} MCP tools; kiln-pro adds "
            f"{split['pro']} more (product generators, decoration, fleet ops, "
            f"billing). When kiln-pro is installed, agents see {split['total']} "
            f"live tools and {mcp_counts['total']} total MCP capabilities "
            f"including {mcp_counts['prompts']} prompts and "
            f"{mcp_counts['resources']} resources. When it isn't, agents still "
            "see manifest stubs for pro tools labeled with their tier + "
            "upgrade URL so they can recommend upgrades."
        )
    else:
        # Registry not reachable (cold import path). Keep the copy
        # honest: no hardcoded numbers, just the shape of the system.
        total_tools_note = (
            "Public Kiln ships a broad MCP tool catalog; kiln-pro extends "
            "it with product generators, decoration, fleet ops, and billing. "
            "When kiln-pro isn't installed, agents still see manifest stubs "
            "for pro tools labeled with their tier + upgrade URL so they can "
            "recommend upgrades."
        )

    return {
        "total_tools_note": total_tools_note,
        "pattern": (
            "MCP clients don't load all tool schemas upfront at this "
            "scale. Agents use ToolSearch(keyword) to surface tool schemas "
            "on demand — e.g. ToolSearch('slice bambu'), "
            "ToolSearch('ams filament'), ToolSearch('billing')."
        ),
        "entry_points": [
            "get_started() — quick-start + live tool/capability count + core workflows",
            "get_skill_manifest() — this tool; full capability map",
            "printer_status() — first concrete probe for any agent",
        ],
        "tier_visibility_for_agents": (
            "Tier-gated tools appear in ToolSearch results with a tier "
            "label ('Requires Kiln Pro'/'Business') and upgrade URL in "
            "the description. Free-tier agents can surface these to users "
            "for upgrade messaging without kiln-pro installed locally."
        ),
    }


def get_tool_counts_split() -> dict[str, int]:
    """Return ``{"public": N, "pro": N, "total": N}`` from the live registry.

    Introspects each registered MCP tool's ``__module__`` to classify
    it as a public Kiln tool (``kiln.*``) or kiln-pro plugin tool
    (``kiln_pro.*``).  Used to render "public Kiln ships N tools;
    kiln-pro adds M more" copy dynamically.  Returns zeros if the
    registry isn't reachable.
    """
    out = {"public": 0, "pro": 0, "total": 0}
    try:
        import kiln.server as _srv
    except Exception:  # noqa: BLE001
        return out

    try:
        tools = _srv.mcp._tool_manager._tools  # dict[name, Tool]
    except Exception:  # noqa: BLE001
        return out

    for tool in tools.values():
        fn = getattr(tool, "fn", None) or getattr(tool, "func", None) or getattr(tool, "handler", None)
        mod = getattr(fn, "__module__", "") if fn is not None else ""
        if mod.startswith("kiln_pro"):
            out["pro"] += 1
        elif mod.startswith("kiln"):
            out["public"] += 1
        # else: third-party plugin — ignored for the public/pro split
    out["total"] = out["public"] + out["pro"]
    return out


def generate_manifest() -> SkillManifest:
    """Generate a complete skill manifest."""
    mcp_counts = get_mcp_capability_counts()
    return SkillManifest(
        version=get_version(),
        tool_count=mcp_counts["tools"],
        mcp_capability_count=mcp_counts["total"],
    )


def get_skill_definition_path() -> Path:
    """Return the path to the bundled SKILL.md file.

    Searches common locations relative to the package and the user home
    directory.  Raises :class:`FileNotFoundError` if no SKILL.md is found.
    """
    candidates = [
        Path(__file__).resolve().parent.parent.parent.parent / "SKILL.md",
        Path(__file__).resolve().parent / "data" / "SKILL.md",
        Path.home() / ".kiln" / "SKILL.md",
        # Repo .dev/ location
        Path(__file__).resolve().parent.parent.parent.parent / ".dev" / "SKILL.md",
    ]
    for p in candidates:
        if p.is_file():
            return p
    raise FileNotFoundError("SKILL.md not found. Run 'pip install kiln3d' or check your installation.")


_AGENT_WORKSPACE_MARKERS: list[str] = [
    "CLAUDE.md",  # Claude Code
    "claude.yaml",  # Claude Desktop
    ".cursorrules",  # Cursor
    ".windsurfrules",  # Windsurf
    "AGENTS.md",  # Generic agent workspace
    ".github/copilot",  # GitHub Copilot
]


def detect_agent_workspaces(*, search_dir: str | None = None) -> list[dict[str, Any]]:
    """Detect AI agent workspaces in common locations.

    Searches for marker files that indicate an agent workspace.
    Returns a list of dicts with path, agent_type, and marker info.
    """
    results: list[dict[str, Any]] = []

    search_paths: list[Path] = []
    if search_dir:
        search_paths.append(Path(search_dir))
    else:
        home = Path.home()
        search_paths.extend(
            [
                Path.cwd(),
                home / "Documents",
                home / "Projects",
                home / "Code",
                home / "Developer",
                home / "dev",
            ]
        )

    seen: set[str] = set()
    for base in search_paths:
        if not base.is_dir():
            continue
        _scan_dir_for_markers(base, seen, results)
        # One level of subdirectories
        try:
            for entry in base.iterdir():
                if entry.is_dir():
                    _scan_dir_for_markers(entry, seen, results)
        except PermissionError:
            continue

    return results


def _scan_dir_for_markers(
    directory: Path,
    seen: set[str],
    results: list[dict[str, Any]],
) -> None:
    """Check *directory* for agent workspace marker files."""
    resolved = str(directory.resolve())
    if resolved in seen:
        return
    seen.add(resolved)

    for marker in _AGENT_WORKSPACE_MARKERS:
        marker_path = directory / marker
        if marker_path.exists():
            results.append(
                {
                    "path": str(directory),
                    "agent_type": _marker_to_agent_type(marker),
                    "marker": marker,
                    "skill_installed": _check_skill_installed(directory),
                }
            )
            break


def _marker_to_agent_type(marker: str) -> str:
    """Map a marker filename to an agent type name."""
    mapping: dict[str, str] = {
        "CLAUDE.md": "claude_code",
        "claude.yaml": "claude_desktop",
        ".cursorrules": "cursor",
        ".windsurfrules": "windsurf",
        "AGENTS.md": "generic",
        ".github/copilot": "copilot",
    }
    return mapping.get(marker, "unknown")


def _check_skill_installed(workspace: Path) -> bool:
    """Check if the Kiln skill is already installed in a workspace."""
    skill_locations = [
        workspace / ".dev" / "SKILL.md",
        workspace / "SKILL.md",
        workspace / ".kiln" / "SKILL.md",
    ]
    return any(p.is_file() for p in skill_locations)


def install_skill(workspace_path: str, *, force: bool = False) -> dict[str, Any]:
    """Install the Kiln skill definition into an agent workspace.

    Copies SKILL.md to the appropriate location based on workspace layout.
    Returns a dict with installation result info.
    """
    workspace = Path(workspace_path)
    if not workspace.is_dir():
        return {"success": False, "error": f"Workspace not found: {workspace_path}"}

    try:
        source = get_skill_definition_path()
    except FileNotFoundError as exc:
        return {"success": False, "error": str(exc)}

    # Prefer .dev/ if it exists, otherwise workspace root
    dev_dir = workspace / ".dev"
    if dev_dir.is_dir():
        target = dev_dir / "SKILL.md"
    else:
        target = workspace / "SKILL.md"

    if target.is_file() and not force:
        return {
            "success": False,
            "error": f"SKILL.md already exists at {target}. Use --force to overwrite.",
            "existing_path": str(target),
        }

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(target))

    return {
        "success": True,
        "installed_path": str(target),
        "source_path": str(source),
    }
