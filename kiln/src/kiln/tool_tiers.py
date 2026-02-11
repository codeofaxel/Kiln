"""Tool tier definitions for different model capabilities.

Defines three tiers of MCP tools for use with language models of varying
capability.  Weaker models get fewer, simpler tools to avoid overwhelming
their context window and function-calling accuracy.  Stronger models get
the full set.

Tiers
-----
``essential``
    15 core tools for weak models (Llama, Mistral, Phi, Qwen).
``standard``
    ~40 tools for capable models (GPT-4o-mini, Gemini Flash, Command R+).
``full``
    All 101 tools for strong models (Claude, GPT-4, Gemini Pro).
"""

from __future__ import annotations

from typing import Dict, List


# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------

TIER_ESSENTIAL: List[str] = [
    "printer_status",
    "printer_files",
    "upload_file",
    "start_print",
    "cancel_print",
    "pause_print",
    "resume_print",
    "set_temperature",
    "preflight_check",
    "send_gcode",
    "fleet_status",
    "submit_job",
    "job_status",
    "queue_summary",
    "kiln_health",
]

TIER_STANDARD: List[str] = TIER_ESSENTIAL + [
    # Marketplace & models
    "search_all_models",
    "download_model",
    "download_and_upload",
    "model_details",
    "marketplace_info",
    # Slicing
    "slice_model",
    "slice_and_print",
    "find_slicer_tool",
    # Cost & materials
    "estimate_cost",
    "list_materials",
    "set_material",
    "get_material",
    "check_material_match",
    # Monitoring
    "printer_snapshot",
    "webcam_stream",
    # Fleet management
    "discover_printers",
    "register_printer",
    # Validation
    "validate_gcode",
    "validate_gcode_safe",
    # Workflow helpers
    "await_print_completion",
    "compare_print_options",
    # Safety profiles
    "list_safety_profiles",
    "get_safety_profile",
    # History & stats
    "print_history",
    "printer_stats",
    # File & job management
    "delete_file",
    "job_history",
    "cancel_job",
]

TIER_FULL: List[str] = [
    # --- Core printer control ---
    "printer_status",
    "printer_files",
    "upload_file",
    "delete_file",
    "start_print",
    "cancel_print",
    "pause_print",
    "resume_print",
    "set_temperature",
    "preflight_check",
    "send_gcode",
    "validate_gcode",
    # --- Fleet ---
    "fleet_status",
    "register_printer",
    "discover_printers",
    # --- Job queue ---
    "submit_job",
    "job_status",
    "queue_summary",
    "cancel_job",
    "job_history",
    # --- Events ---
    "recent_events",
    # --- Billing ---
    "billing_summary",
    "billing_setup_url",
    "billing_status",
    "billing_history",
    # --- Marketplace ---
    "search_all_models",
    "marketplace_info",
    "search_models",
    "model_details",
    "model_files",
    "download_model",
    "download_and_upload",
    "browse_models",
    "list_model_categories",
    # --- Slicing ---
    "slice_model",
    "find_slicer_tool",
    "slice_and_print",
    # --- Monitoring & snapshots ---
    "printer_snapshot",
    "webcam_stream",
    # --- Cost estimation ---
    "estimate_cost",
    # --- Materials ---
    "list_materials",
    "set_material",
    "get_material",
    "check_material_match",
    "list_spools",
    "add_spool",
    "remove_spool",
    # --- Bed leveling ---
    "bed_level_status",
    "trigger_bed_level",
    "set_leveling_policy",
    # --- Cloud sync ---
    "cloud_sync_status",
    "cloud_sync_now",
    "cloud_sync_configure",
    # --- Plugins ---
    "list_plugins",
    "plugin_info",
    # --- Fulfillment ---
    "fulfillment_materials",
    "fulfillment_quote",
    "fulfillment_order",
    "fulfillment_order_status",
    "fulfillment_cancel",
    # --- Health ---
    "kiln_health",
    # --- Webhooks ---
    "register_webhook",
    "list_webhooks",
    "delete_webhook",
    # --- Workflow helpers ---
    "await_print_completion",
    "compare_print_options",
    "analyze_print_failure",
    "validate_print_quality",
    # --- Generation ---
    "list_generation_providers",
    "generate_model",
    "generation_status",
    "download_generated_model",
    "await_generation",
    "generate_and_print",
    "validate_generated_mesh",
    # --- Firmware ---
    "firmware_status",
    "update_firmware",
    "rollback_firmware",
    # --- Print history & stats ---
    "print_history",
    "printer_stats",
    "annotate_print",
    # --- Vision monitoring ---
    "monitor_print_vision",
    "watch_print",
    # --- Cross-printer learning ---
    "record_print_outcome",
    "get_printer_insights",
    "suggest_printer_for_job",
    # --- Agent memory ---
    "save_agent_note",
    "get_agent_context",
    "delete_agent_note",
    # --- Safety profiles ---
    "list_safety_profiles",
    "get_safety_profile",
    "validate_gcode_safe",
    # --- Slicer profiles ---
    "list_slicer_profiles_tool",
    "get_slicer_profile_tool",
    # --- Printer intelligence ---
    "get_printer_intelligence",
    "get_material_recommendation",
    "troubleshoot_printer",
    # --- Pipelines ---
    "list_print_pipelines",
    "run_quick_print",
    "run_calibrate",
    "run_benchmark",
]

TIERS: Dict[str, List[str]] = {
    "essential": TIER_ESSENTIAL,
    "standard": TIER_STANDARD,
    "full": TIER_FULL,
}


# ---------------------------------------------------------------------------
# Tier lookup
# ---------------------------------------------------------------------------

def get_tier(name: str) -> List[str]:
    """Return tool names for a tier.

    Args:
        name: Tier name — ``"essential"``, ``"standard"``, or ``"full"``.

    Returns:
        List of tool name strings belonging to that tier.

    Raises:
        KeyError: If *name* is not a recognised tier.
    """
    try:
        return TIERS[name]
    except KeyError:
        raise KeyError(
            f"Unknown tier {name!r}. Available tiers: {', '.join(sorted(TIERS))}"
        ) from None


# ---------------------------------------------------------------------------
# Model-to-tier suggestion
# ---------------------------------------------------------------------------

# Patterns are checked with str.startswith against the lowercased model name.
_FULL_PREFIXES = (
    "claude",
    "gpt-4o",
    "gpt-4-",
    "gpt-4",
    "gemini-pro",
    "gemini-1.5-pro",
    "gemini-2",
    "o1",
    "o3",
    "o4",
    "deepseek-v3",
    "deepseek-r1",
)

_STANDARD_PREFIXES = (
    "gpt-4o-mini",
    "gpt-3.5",
    "gemini-flash",
    "gemini-1.5-flash",
    "command-r",
    "command-r+",
    "deepseek-v2",
    "yi-",
)

_ESSENTIAL_PREFIXES = (
    "llama",
    "mistral",
    "mixtral",
    "phi",
    "qwen",
    "gemma",
    "tinyllama",
    "codellama",
    "vicuna",
    "openchat",
)


def suggest_tier(model_name: str) -> str:
    """Suggest a tool tier based on model name or ID.

    Matches against known model family prefixes.  Returns ``"standard"``
    as a safe default for unrecognised models.

    Args:
        model_name: Model name or OpenRouter model ID
            (e.g. ``"claude-3-opus"``, ``"meta-llama/llama-3-8b"``).

    Returns:
        Tier name string: ``"essential"``, ``"standard"``, or ``"full"``.
    """
    name = model_name.lower()

    # OpenRouter IDs look like "provider/model-name" — extract the model part.
    if "/" in name:
        name = name.split("/", 1)[1]

    # gpt-4o-mini must be checked before gpt-4o (more specific first).
    for prefix in _STANDARD_PREFIXES:
        if name.startswith(prefix):
            return "standard"

    for prefix in _FULL_PREFIXES:
        if name.startswith(prefix):
            return "full"

    for prefix in _ESSENTIAL_PREFIXES:
        if name.startswith(prefix):
            return "essential"

    # Unknown model — standard is a safe middle ground.
    return "standard"
