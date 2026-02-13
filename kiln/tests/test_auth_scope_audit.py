"""Per-tool auth scope audit -- ensures every mutating MCP tool has an auth scope.

This is a "no tool left behind" test: if a new tool is added to server.py
that performs write/mutation operations, it MUST call ``_check_auth(scope)``
or ``_check_billing_auth(scope)``.  Read-only / informational tools are
explicitly allowlisted and do not require scopes.

Existing mutating tools that predate the auth scope requirement are tracked
in ``KNOWN_UNSCOPED_MUTATING_TOOLS``.  Each one should be migrated to use
``_check_auth()`` and removed from that set.  The test will fail if:

1. A NEW tool appears without auth AND is not in READ_ONLY_TOOLS.
2. An entry in READ_ONLY_TOOLS gains an auth check (misclassified).
3. A stale entry exists in either allowlist (tool was renamed/removed).
4. A tool in KNOWN_UNSCOPED_MUTATING_TOOLS gains auth (remove from set).

Coverage areas:
- Every @mcp.tool() function in server.py is accounted for
- Mutating tools have a _check_auth or _check_billing_auth call
- No new tool is silently added without scope assignment
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Set

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_server_source() -> str:
    """Return the full source text of kiln/server.py."""
    server_path = Path(__file__).resolve().parent.parent / "src" / "kiln" / "server.py"
    return server_path.read_text()


def _extract_tools(source: str) -> dict[str, str]:
    """Extract all @mcp.tool() function names and their bodies from source.

    Returns a dict mapping tool function name -> function body text.
    """
    lines = source.split("\n")
    tools: dict[str, str] = {}
    i = 0
    while i < len(lines):
        if "@mcp.tool()" in lines[i]:
            # Find the def line
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("def "):
                j += 1
            if j < len(lines):
                match = re.match(r"\s*def\s+(\w+)\s*\(", lines[j])
                if match:
                    tool_name = match.group(1)
                    # Collect body until next @mcp.tool() or top-level def/class
                    body_start = j + 1
                    k = body_start
                    while k < len(lines):
                        if "@mcp.tool()" in lines[k]:
                            break
                        if (
                            k > body_start
                            and lines[k].strip()
                            and not lines[k][0].isspace()
                            and (
                                lines[k].startswith("def ")
                                or lines[k].startswith("class ")
                                or lines[k].startswith("@")
                            )
                        ):
                            break
                        k += 1
                    body = "\n".join(lines[body_start:k])
                    tools[tool_name] = body
            i = j + 1 if j > i else i + 1
        else:
            i += 1
    return tools


def _tool_has_auth_check(body: str) -> bool:
    """Return True if the tool body calls _check_auth or _check_billing_auth."""
    return "_check_auth(" in body or "_check_billing_auth(" in body


# ---------------------------------------------------------------------------
# Read-only tools -- intentionally exempt from auth scopes.
#
# These tools only return information and never modify printer state,
# files, jobs, configuration, or any persistent data.
# ---------------------------------------------------------------------------

READ_ONLY_TOOLS: Set[str] = {
    # Printer status / info (read-only queries)
    "printer_status",
    "printer_files",
    "analyze_print_file",
    "preflight_check",
    "validate_gcode",
    "fleet_status",
    "fleet_analytics",
    "recent_events",
    "firmware_status",
    "bed_level_status",
    "cloud_sync_status",
    "list_plugins",
    "plugin_info",
    "list_materials",
    "get_material",
    "check_material_match",
    "list_spools",
    "webcam_stream",
    "printer_snapshot",
    "estimate_cost",
    "kiln_health",
    "health_check",

    # Safety / audit (read-only inspections)
    "safety_audit",
    "safety_status",
    "safety_settings",
    "get_autonomy_level",
    "check_autonomy",

    # Billing read-only
    "billing_summary",
    "billing_check_setup",
    "billing_alerts",

    # Marketplace read-only (searches / metadata)
    "search_all_models",
    "marketplace_info",
    "search_models",
    "model_details",
    "model_files",
    "browse_models",
    "list_model_categories",

    # Slicer discovery (read-only)
    "find_slicer_tool",

    # Fulfillment read-only
    "fulfillment_materials",
    "fulfillment_quote",
    "fulfillment_order_status",
    "fulfillment_alerts",

    # Monitoring / analysis (read-only)
    "list_webhooks",
    "compare_print_options",
    "analyze_print_failure",
    "validate_print_quality",
    "list_generation_providers",
    "validate_generated_mesh",

    # Onboarding / help (no side effects)
    "get_started",
    "confirm_action",

    # Discovery / scan (no mutation)
    "discover_printers",

    # Skill manifest (read-only metadata)
    "get_skill_manifest",

    # Utility read-only helpers
    "find_material_substitute",
    "get_best_material_substitute",
    "extract_file_metadata",
    "estimate_print_progress",
    "fleet_utilization",
    "list_cached_designs",
    "get_cached_design",
    "list_credentials",
    "retrieve_credential",
    "analyze_print_snapshot",
    "get_fulfillment_quote_cached",
    "check_firmware_status",
    "print_status_lite",
    "list_snapshots",
    "fleet_job_status",
    "check_printer_health",
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

KNOWN_UNSCOPED_MUTATING_TOOLS: Set[str] = {
    # Material / spool management
    "set_material",
    "add_spool",
    "remove_spool",

    # Bed leveling control
    "trigger_bed_level",
    "set_leveling_policy",

    # Autonomy / config
    "set_autonomy_level",

    # Cloud sync
    "cloud_sync_now",
    "cloud_sync_configure",

    # Marketplace download (writes to disk)
    "download_model",

    # Slicer (writes files)
    "slice_model",

    # Long-running blocking operations
    "await_print_completion",

    # Print recovery / checkpoint
    "save_print_checkpoint",
    "plan_print_recovery",
    "firmware_resume_print",

    # Health monitoring control
    "start_printer_health_monitoring",
    "stop_printer_health_monitoring",

    # Fleet job routing
    "route_print_job",
    "fleet_submit_job",

    # Cache writes
    "cache_design",

    # Credential store
    "store_credential",

    # Printer locking
    "acquire_printer_lock",
    "release_printer_lock",

    # Firmware update (duplicate of scoped versions)
    "update_printer_firmware",
    "rollback_printer_firmware",
}


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestAuthScopeAudit:
    """Audit that every mutating MCP tool has an auth scope declared."""

    @pytest.fixture(scope="class")
    def tools(self) -> dict[str, str]:
        source = _get_server_source()
        return _extract_tools(source)

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
        # Baseline: 29 tools as of the initial audit.  If this number
        # increases, someone added a mutating tool to the gap set instead
        # of adding proper auth.
        assert current_count <= 29, (
            f"KNOWN_UNSCOPED_MUTATING_TOOLS grew to {current_count} (was 29). "
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
        """Sanity check: at least 50 tools should have auth scopes.

        Catches a broken parser -- if the extractor stops finding auth
        checks, this test will fail.
        """
        auth_count = sum(1 for body in tools.values() if _tool_has_auth_check(body))
        assert auth_count >= 50, (
            f"Expected at least 50 tools with auth scopes, found {auth_count}. "
            f"The source parser may be broken."
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
            "cancel_job",
            "register_webhook",
            "delete_webhook",
            "generate_model",
            "fulfillment_order",
        }
        overlap = critical_write_tools & READ_ONLY_TOOLS
        assert not overlap, (
            f"Critical write tools found in READ_ONLY_TOOLS: {sorted(overlap)}"
        )
