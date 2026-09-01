"""Per-tool auth scope audit -- ensures every mutating MCP tool has an auth scope.

This is a "no tool left behind" test: if a new tool is added to server.py
OR any plugin module under ``kiln/plugins/`` that performs write/mutation
operations, it MUST call ``_check_auth(scope)`` or
``_check_billing_auth(scope)``.  Read-only / informational tools are
explicitly allowlisted and do not require scopes.

Existing mutating tools that predate the auth scope requirement are tracked
in ``KNOWN_UNSCOPED_MUTATING_TOOLS``.  Each one should be migrated to use
``_check_auth()`` and removed from that set.  The test will fail if:

1. A NEW tool appears without auth AND is not in READ_ONLY_TOOLS.
2. An entry in READ_ONLY_TOOLS gains an auth check (misclassified).
3. A stale entry exists in either allowlist (tool was renamed/removed).
4. A tool in KNOWN_UNSCOPED_MUTATING_TOOLS gains auth (remove from set).
5. A tool is gated with a scope no key can satisfy (an unreachable tool
   is as broken as an ungated one -- see test_every_scope_is_satisfiable).

Coverage areas:
- Every @mcp.tool() function in server.py AND kiln/plugins/*.py is accounted for
- Mutating tools have a _check_auth or _check_billing_auth call
- No new tool is silently added without scope assignment
- Every scope in use is resolvable by kiln.auth
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool_source_files() -> list[Path]:
    """Every file that registers @mcp.tool() functions: server.py + plugins."""
    src = Path(__file__).resolve().parent.parent / "src" / "kiln"
    return [src / "server.py"] + sorted((src / "plugins").glob("*.py"))


def _is_mcp_tool_decorator(dec: ast.expr) -> bool:
    """Match ``@mcp.tool()`` (with or without arguments)."""
    return (
        isinstance(dec, ast.Call)
        and isinstance(dec.func, ast.Attribute)
        and dec.func.attr == "tool"
        and isinstance(dec.func.value, ast.Name)
        and dec.func.value.id == "mcp"
    )


def _extract_all_tools() -> dict[str, str]:
    """Extract every @mcp.tool() function name -> body text across all files.

    Uses the AST so tools nested inside a plugin's ``register()`` method are
    found exactly, and a tool body never bleeds into the next function (the
    old line-based scanner only understood top-level server.py tools --
    that blind spot is how ungated plugin tools shipped).
    """
    tools: dict[str, str] = {}
    duplicates: list[str] = []
    for path in _tool_source_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(_is_mcp_tool_decorator(d) for d in node.decorator_list):
                continue
            if node.name in tools:
                duplicates.append(node.name)
            tools[node.name] = ast.unparse(node)
    assert not duplicates, (
        f"Tool function names registered more than once across server.py "
        f"and plugins (each would shadow the other in this audit and "
        f"collide at MCP registration): {sorted(duplicates)}"
    )
    return tools


def _tool_has_auth_check(body: str) -> bool:
    """Return True if the tool body calls _check_auth or _check_billing_auth."""
    return "_check_auth(" in body or "_check_billing_auth(" in body


def _scopes_used(body: str) -> set[str]:
    """Return the scope strings a tool body passes to the auth checks."""
    return set(
        re.findall(r"_check_(?:billing_)?auth\(\s*['\"]([a-z_]+)['\"]", body)
    )


# ---------------------------------------------------------------------------
# Read-only tools -- intentionally exempt from auth scopes.
#
# These tools only return information and never modify printer state,
# files, jobs, configuration, or any persistent data.
# ---------------------------------------------------------------------------

READ_ONLY_TOOLS: set[str] = {
    # Printer status / info (read-only queries)
    "printer_status",
    "printer_files",
    "analyze_print_file",
    "preflight_check",
    "fleet_status",
    "recent_events",
    "bed_level_status",
    "list_plugins",
    "list_materials",
    "get_material",
    "check_material_match",
    "list_spools",
    "webcam_stream",
    "printer_snapshot",

    # Safety / audit (read-only inspections)
    "get_autonomy_level",
    "check_autonomy",

    # Billing read-only

    # License read-only
    "license_status",

    # Marketplace read-only (searches / metadata)
    "marketplace_info",

    # Slicer discovery (read-only)

    # Fulfillment read-only

    # Monitoring / analysis (read-only)
    "list_webhooks",
    "compare_print_options",
    "analyze_print_failure",
    "render_model_preview",
    "visualize_model",
    "get_feedback_loop_status",
    "list_design_templates",
    "validate_openscad_code",

    # Onboarding / help (no side effects)

    # Discovery / scan (no mutation)

    # Skill manifest (read-only metadata)

    # Utility read-only helpers
    "find_material_substitute",
    "get_best_material_substitute",
    "extract_file_metadata",
    "analyze_print_snapshot",
    "get_fulfillment_quote_cached",
    "print_status_lite",
    "list_snapshots",
    "check_printer_health",

    # Fleet site grouping / cost reporting (read-only)
    "project_cost_summary",
    "client_cost_report",

    # Database / infrastructure status (read-only)

    # Ambient / trend analysis (read-only)
    "check_ambient_conditions",
    "printer_trend_analysis",

    # Orientation / monitoring (read-only)
    "monitor_print",
    "check_orientation",

    # Design generation analysis — Phase 4 (read-only)
    "predict_print_failure",

    # Design generation analysis — Phase 5 (read-only)

    # Design generation analysis — Phase 6 (read-only)

    # Design reasoning engine (read-only structural analysis)

    # Render comparison (read-only)
    "compare_renders",

    # ------------------------------------------------------------------
    # Plugin tools (kiln/plugins/*.py) -- judged individually when the
    # audit was extended to cover plugin files.  Every entry below only
    # returns information / pure computed results and never modifies
    # printer state, files, jobs, configuration, or persistent data.
    # ------------------------------------------------------------------

    # assembly_tools.py -- assembly state is a JSON value passed in and
    # returned; nothing is persisted (compose_assembly_parts, which
    # writes an STL, is gated)
    "add_assembly_interface",
    "add_assembly_part",
    "check_assembly_clearances",
    "create_assembly",
    "get_joint_recommendation",
    "validate_assembly",

    # cloud_sync_tools.py
    "cloud_sync_status",

    # consumer_tools.py -- estimates, tax lookups, onboarding guides
    "consumer_onboarding",
    "donate_info",
    "estimate_price",
    "estimate_timeline",
    "suggest_material_for_order",
    "supported_shipping_countries",
    "tax_estimate",
    "tax_jurisdiction_lookup",
    "tax_jurisdictions",
    "validate_shipping_address",

    # credential_tools.py -- metadata only, never plaintext
    # (retrieve_credential returns decrypted secrets and is gated)
    "list_credentials",

    # decoration_library_tools.py -- library queries
    "decoration_info",
    "decoration_quota_status",
    "list_decorations",

    # design_reasoning_tools.py -- structural analysis / recommendations
    "analyze_structural_risks",
    "assess_load_bearing",
    "design_advisor",
    "design_improvement_plan",
    "estimate_support_material",
    "infer_print_settings",
    "recommend_design_reinforcements",

    # design_tools.py -- analysis, lookups, prompt builders, and pure
    # SCAD string transforms (code in, code out -- nothing written;
    # compile_scad / tweak_and_compile_scad / cache_design_with_source
    # write artifacts and are gated)
    "analyze_design_requirements",
    "analyze_scad_code",
    "analyze_warping_risk",
    "audit_original_design",
    "build_generation_prompt",
    "build_parametric_prompt",
    "check_material_environment",
    "check_multi_material_pairing",
    "check_printer_material_compatibility",
    "estimate_print_cost_from_mesh",
    "estimate_structural_load",
    "find_design_templates",
    "get_design_source",
    "get_design_template_info",
    "get_material_design_profile",
    "get_post_processing_guide",
    "get_print_diagnostic",
    "insert_into_scad",
    "list_design_components",
    "list_design_materials",
    "list_design_templates_catalog",
    "match_design_components",
    "match_design_requirements",
    "modify_scad_module",
    "parse_scad_parameters",
    "recommend_design_material",
    "resolve_filament_profile",
    "troubleshoot_print_issue",
    "update_scad_parameter",
    "validate_design_for_requirements",
    "validate_scad_parameters",

    # enterprise_tools.py -- status query (SELECT COUNT only)
    "database_status",

    # estimate_tools.py -- estimates from geometry or already-sliced
    # G-code.  The two doors that actually invoke the slicer
    # (slice_and_estimate, estimate_print_time) are gated instead.
    "estimate_before_design",
    "estimate_cost",
    "estimate_material_cost",
    "estimate_print_progress",
    "list_multi_material_addons",

    # firmware_tools.py -- status queries
    "check_firmware_status",
    "firmware_status",

    # fleet_tools.py -- analytics / status queries
    "fleet_analytics",
    "fleet_job_status",
    "fleet_status_by_site",
    "fleet_utilization",
    "list_fleet_sites",

    # fulfillment_tools.py -- quotes, listings, status (order placement,
    # profile writes, and token issuance are gated)
    "fulfillment_alerts",
    "fulfillment_materials",
    "fulfillment_order_status",
    "fulfillment_quote",
    "list_shipping_profiles",

    # gcode_validation_tools.py -- validate_print_quality's only write
    # is an optional user-requested snapshot export under home/tmp,
    # same class as the allowlisted printer_snapshot
    "validate_gcode",
    "validate_print_quality",

    # generation_ai_tools.py
    "list_generation_providers",

    # generation_tools.py -- renders preview PNGs, like
    # render_model_preview above
    "preview_generated_model",

    # intelligence_tools.py -- queries and pure fingerprint computation
    # (record_print_dna / contribute_community_print write and are gated)
    "community_stats",
    "find_similar_prints",
    "fingerprint_model",
    "get_community_insight",
    "get_model_print_history",
    "list_available_materials",
    "predict_print_settings",
    "recommend_material",

    # marketplace_tools.py -- browsing / search / connectivity checks
    "browse_models",
    "list_model_categories",
    "marketplace_diagnostics",
    "marketplace_status",
    "model_details",
    "model_files",
    "search_all_models",
    "search_models",

    # material_catalog_tools.py -- catalog queries
    "find_material_match",
    "get_compatible_materials",
    "get_material_info",
    "get_material_purchase_urls",
    "list_material_catalog",
    "search_material_catalog",

    # material_inventory_tools.py -- inventory queries and pure
    # assignment/swap recommendations (nothing is submitted)
    "check_material_sufficiency",
    "find_printers_with_material",
    "forecast_material_consumption",
    "get_fleet_material_summary",
    "get_material_consumption_history",
    "get_restock_suggestions",
    "optimize_fleet_assignment",
    "suggest_spool_swaps",

    # material_tools.py -- status queries
    "check_print_health",
    "get_active_material",

    # mesh_diagnostic_tools.py
    "diagnose_mesh",

    # mesh_tools.py -- analysis only (repair/transform siblings are gated)
    "analyze_mesh_geometry",
    "analyze_non_manifold_edges",
    "compare_mesh_versions",
    "estimate_mesh_print_time",
    "mesh_quality_scorecard",
    "validate_generated_mesh",

    # network_tools.py -- provider queries (connect/submit/sync are gated)
    "find_provider_capacity",
    "list_provider_capacity",
    "provider_job_status",

    # printability_tools.py -- analysis (auto_orient_model can write a
    # reoriented STL and is gated)
    "analyze_printability",
    "diagnose_print_failure_live",
    "estimate_supports",
    "recommend_adhesion_settings",

    # printer_management_tools.py -- network scan, registers nothing
    "discover_printers",

    # queue_tools.py
    "job_history",

    # safety_tools.py -- log/settings queries
    "safety_audit",
    "safety_settings",
    "safety_status",

    # service_tools.py -- reports built from history, nothing written
    "generate_print_certificate",
    "list_published_models",
    "model_revenue",
    "print_service_status",
    "revenue_dashboard",

    # slicer_tools.py -- availability check (registered as find_slicer)
    "find_slicer_tool",

    # step_tools.py -- queries (import_step_file writes converted
    # meshes and is gated)
    "check_step_support",
    "step_file_info",

    # tier_diagnostic_tools.py
    "check_my_tier",

    # utility_tools.py -- guides, manifests, health queries
    # (trim_serve_processes / upgrade_kiln mutate the system, gated)
    "get_session_log",
    "get_skill_manifest",
    "get_started",
    "health_check",
    "kiln_health",
    "plugin_info",

    # version_tools.py -- version queries and diffs (save/rollback
    # persist versions and are gated)
    "diff_design_versions",
    "get_design_version",
    "list_design_versions",
    "search_design_versions",
}


# ---------------------------------------------------------------------------
# Known unscoped mutating tools -- technical debt from before the auth audit.
#
# Each tool here performs a write/mutation operation but does NOT yet call
# _check_auth().  When you add auth to one of these tools, REMOVE it from
# this set so the test tracks progress.  The test will fail if a tool in
# this set gains auth (prompting removal), or if a NEW mutating tool is
# added without being in either allowlist.
# ---------------------------------------------------------------------------

KNOWN_UNSCOPED_MUTATING_TOOLS: set[str] = {
    "restart_server",

    # Auth bootstrap (auth_tools.py) -- these START the sign-in flow and
    # write the resulting token file, so they are mutating, but requiring
    # auth on them would lock out every not-yet-signed-in user
    # (chicken-and-egg).  Deliberately exempt; do NOT migrate these two.
    "kiln_signin",
    "kiln_signin_poll",
}


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestAuthScopeAudit:
    """Audit that every mutating MCP tool has an auth scope declared."""

    @pytest.fixture(scope="class")
    def tools(self) -> dict[str, str]:
        return _extract_all_tools()

    def test_all_tools_accounted_for(self, tools: dict[str, str]):
        """Every tool must be in one of: has auth, READ_ONLY_TOOLS, or KNOWN_UNSCOPED."""
        unaccounted = []
        for name, body in tools.items():
            has_auth = _tool_has_auth_check(body)
            is_read_only = name in READ_ONLY_TOOLS
            is_known_gap = name in KNOWN_UNSCOPED_MUTATING_TOOLS
            if not has_auth and not is_read_only and not is_known_gap:
                unaccounted.append(name)

        assert not unaccounted, (
            f"New tools missing auth scope: {sorted(unaccounted)}. "
            f"Add _check_auth('scope') to the tool, add it to READ_ONLY_TOOLS "
            f"if truly read-only, or add it to KNOWN_UNSCOPED_MUTATING_TOOLS "
            f"(temporary) in test_auth_scope_audit.py."
        )

    def test_known_unscoped_tools_still_lack_auth(self, tools: dict[str, str]):
        """Tools in KNOWN_UNSCOPED_MUTATING_TOOLS that gain auth should be removed.

        This tracks progress: when you add _check_auth() to a tool, remove
        it from KNOWN_UNSCOPED_MUTATING_TOOLS.
        """
        now_scoped = []
        for name in KNOWN_UNSCOPED_MUTATING_TOOLS:
            if name in tools and _tool_has_auth_check(tools[name]):
                now_scoped.append(name)

        assert not now_scoped, (
            f"Tools in KNOWN_UNSCOPED_MUTATING_TOOLS that now have auth "
            f"(remove them from the set): {sorted(now_scoped)}"
        )

    def test_known_unscoped_count_only_shrinks(self, tools: dict[str, str]):
        """Track the size of the debt backlog -- it should only decrease over time."""
        current_count = len(KNOWN_UNSCOPED_MUTATING_TOOLS)
        # Baseline: 3 as of the plugin-audit extension (restart_server plus
        # the two deliberately exempt auth-bootstrap tools).  If this number
        # increases, someone added a mutating tool to the gap set instead
        # of adding proper auth.
        assert current_count <= 3, (
            f"KNOWN_UNSCOPED_MUTATING_TOOLS grew to {current_count} (was 3). "
            f"Add _check_auth() to the new tool instead of adding it to the debt set."
        )

    def test_read_only_tools_have_no_auth(self, tools: dict[str, str]):
        """Tools in READ_ONLY_TOOLS should NOT have auth checks."""
        misclassified = []
        for name in READ_ONLY_TOOLS:
            if name in tools and _tool_has_auth_check(tools[name]):
                misclassified.append(name)

        assert not misclassified, (
            f"Tools in READ_ONLY_TOOLS that now have auth checks "
            f"(remove from allowlist): {sorted(misclassified)}"
        )

    def test_no_stale_read_only_entries(self, tools: dict[str, str]):
        """Every entry in READ_ONLY_TOOLS must correspond to an actual tool."""
        stale = READ_ONLY_TOOLS - set(tools.keys())
        assert not stale, (
            f"READ_ONLY_TOOLS contains entries that are not registered tools "
            f"(remove them): {sorted(stale)}"
        )

    def test_no_stale_known_unscoped_entries(self, tools: dict[str, str]):
        """Every entry in KNOWN_UNSCOPED_MUTATING_TOOLS must be an actual tool."""
        stale = KNOWN_UNSCOPED_MUTATING_TOOLS - set(tools.keys())
        assert not stale, (
            f"KNOWN_UNSCOPED_MUTATING_TOOLS contains entries that are not "
            f"registered tools (remove them): {sorted(stale)}"
        )

    def test_no_overlap_between_sets(self):
        """READ_ONLY_TOOLS and KNOWN_UNSCOPED_MUTATING_TOOLS must not overlap."""
        overlap = READ_ONLY_TOOLS & KNOWN_UNSCOPED_MUTATING_TOOLS
        assert not overlap, (
            f"Tools appear in both READ_ONLY_TOOLS and "
            f"KNOWN_UNSCOPED_MUTATING_TOOLS: {sorted(overlap)}"
        )

    def test_mutating_tools_with_scopes_detected(self, tools: dict[str, str]):
        """Sanity check: at least 200 tools should have auth scopes.

        Catches a broken parser -- if the extractor stops finding auth
        checks (or stops seeing the plugin files), this test will fail.
        244 tools carried scopes when the audit was extended to plugins.
        """
        auth_count = sum(1 for body in tools.values() if _tool_has_auth_check(body))
        assert auth_count >= 200, (
            f"Expected at least 200 tools with auth scopes, found {auth_count}. "
            f"The source parser may be broken."
        )

    def test_every_scope_is_satisfiable(self, tools: dict[str, str]):
        """A gate that denies everyone is as broken as a missing gate.

        ``_check_auth`` normalizes an unrecognized scope to itself, and an
        unrecognized scope is in no key's expanded scope set -- so a tool
        gated with a scope missing from ``_SCOPE_ALIAS_TO_CANONICAL`` is
        unreachable for EVERY key, including a full admin key.  Every scope
        a tool actually passes must be satisfiable by an admin key.
        """
        from kiln.auth import _scope_satisfied

        admin_key_scopes = {"read", "write", "admin"}
        unsatisfiable: dict[str, list[str]] = {}
        for name, body in tools.items():
            for scope in _scopes_used(body):
                if not _scope_satisfied(scope, admin_key_scopes):
                    unsatisfiable.setdefault(scope, []).append(name)

        assert not unsatisfiable, (
            "Tools are gated with scopes that no key can satisfy -- these "
            "tools are unreachable whenever auth is enabled. Add the scope "
            "to _SCOPE_ALIAS_TO_CANONICAL in kiln/auth.py (mapping it to "
            "'write' for mutating tools, 'read' for queries): "
            + ", ".join(
                f"{scope} ({len(names)} tools, e.g. {sorted(names)[0]})"
                for scope, names in sorted(unsatisfiable.items())
            )
        )

    def test_plugin_tools_are_scanned(self, tools: dict[str, str]):
        """Sanity check: the audit must see the plugin tools, not just server.py.

        Guards the exact blind spot this audit once had: tools extracted
        to kiln/plugins/*.py were invisible to a server.py-only scan, and
        ungated tools shipped.  400+ tools existed across both locations
        when the audit was extended.
        """
        assert len(tools) >= 400, (
            f"Only {len(tools)} tools found -- the plugin files are "
            f"probably not being scanned (server.py alone has ~124)."
        )

    def test_critical_write_tools_not_in_read_only(self):
        """Known write/mutation tools must never appear in READ_ONLY_TOOLS."""
        critical_write_tools = {
            "upload_file",
            "start_print",
            "cancel_print",
            "delete_file",
            "send_gcode",
            "set_temperature",
            "register_printer",
            "submit_job",
            "cancel_queued_job",
            "register_webhook",
            "delete_webhook",
            "generate_model",
            "fulfillment_order",
            # Plugin-side critical writes
            "retrieve_credential",
            "upgrade_kiln",
            "trim_serve_processes",
            "submit_provider_job",
            "save_shipping_profile",
            "retry_print_with_fix",
            "repair_mesh",
            "rollback_design_version",
        }
        overlap = critical_write_tools & READ_ONLY_TOOLS
        assert not overlap, (
            f"Critical write tools found in READ_ONLY_TOOLS: {sorted(overlap)}"
        )
