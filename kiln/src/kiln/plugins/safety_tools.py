"""Safety tools plugin.

Extracts safety-domain MCP tools from server.py into a focused plugin
module.  Provides tools for safety auditing, safety dashboard, safety
settings display, and safety profile management.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_logger = logging.getLogger(__name__)


class _SafetyToolsPlugin:
    """Safety audit, dashboard, settings, and profile management tools.

    Tools:
        - safety_audit
        - safety_status
        - safety_settings
        - list_safety_profiles
        - get_safety_profile
        - add_safety_profile
    """

    @property
    def name(self) -> str:
        return "safety_tools"

    @property
    def description(self) -> str:
        return "Safety audit, dashboard, settings, and profile management tools"

    def register(self, mcp: Any) -> None:  # noqa: PLR0915
        """Register safety tools with the MCP server."""

        import kiln.server as _srv

        # ------------------------------------------------------------------
        # safety_audit
        # ------------------------------------------------------------------

        @mcp.tool()
        def safety_audit(
            action: str | None = None,
            tool_name: str | None = None,
            limit: int = 25,
        ) -> dict:
            """Query the safety audit log.

            Returns a record of all safety-relevant operations: tool executions,
            blocked attempts, rate-limit violations, and preflight failures.

            Args:
                action: Filter by action type.  Options: ``"executed"``,
                    ``"blocked"``, ``"rate_limited"``, ``"auth_denied"``,
                    ``"preflight_failed"``, ``"dry_run"``.  Omit for all.
                tool_name: Filter by MCP tool name (e.g. ``"send_gcode"``).
                limit: Maximum number of records to return (default 25, max 100).
            """
            limit = min(max(1, limit), 100)
            try:
                db = _srv.get_db()
                entries = db.query_audit(action=action, tool_name=tool_name, limit=limit)
                summary = db.audit_summary()
                return {
                    "success": True,
                    "entries": entries,
                    "summary": summary,
                }
            except Exception as exc:
                _logger.exception("Unexpected error in safety_audit")
                return _srv._error_dict(
                    f"Unexpected error in safety_audit: {exc}", code="INTERNAL_ERROR"
                )

        # ------------------------------------------------------------------
        # safety_status
        # ------------------------------------------------------------------

        @mcp.tool()
        def safety_status() -> dict:
            """Get a comprehensive snapshot of all active safety measures.

            Returns a single summary showing: the active safety profile, temperature
            limits, rate-limit configuration, recent blocked actions, authentication
            status, and confirmation-mode status.  Use this to answer "is my printer
            safe right now?" in a single call.
            """
            try:
                # Active safety profile
                profile_info: dict[str, Any] = {
                    "printer_model": _srv._PRINTER_MODEL or "not configured"
                }
                max_tool, max_bed = _srv._get_temp_limits()
                profile_info["max_hotend_temp"] = max_tool
                profile_info["max_bed_temp"] = max_bed
                if _srv._PRINTER_MODEL:
                    try:
                        from kiln.safety_profiles import (
                            get_profile,
                            limits_provenance_note,
                        )

                        profile = get_profile(_srv._PRINTER_MODEL)
                        profile_info["profile_id"] = profile.id
                        profile_info["display_name"] = profile.display_name
                        profile_info["max_feedrate"] = profile.max_feedrate
                        if profile.build_volume:
                            profile_info["build_volume"] = profile.build_volume
                        # This snapshot quotes the limits in force, so it
                        # says whose numbers they are, same as every other
                        # surface that quotes them.
                        profile_info["limits_provenance"] = limits_provenance_note(profile)
                    except KeyError:
                        profile_info["profile_id"] = "default (no specific profile found)"

                # Rate limit configuration
                rate_limits = {}
                for t_name, (interval_ms, per_min) in _srv._TOOL_RATE_LIMITS.items():
                    rate_limits[t_name] = f"{interval_ms}ms cooldown, {per_min}/min"

                # Confirm-level tools (from tool_safety.json)
                confirm_tools = sorted(
                    name
                    for name, meta in _srv._TOOL_SAFETY.items()
                    if meta.get("level") in ("confirm", "emergency")
                )

                # Auth status
                auth_info = {
                    "enabled": (
                        _srv._get_auth().enabled
                        if hasattr(_srv._get_auth(), "enabled")
                        else False
                    ),
                }

                # Confirm mode
                confirm_mode = os.environ.get("KILN_CONFIRM_MODE", "").lower() in (
                    "1",
                    "true",
                    "yes",
                )

                # Recent blocked actions (from audit log)
                recent_blocked: list[dict[str, Any]] = []
                try:
                    db = _srv.get_db()
                    summary = db.audit_summary(window_seconds=3600.0)
                    recent_blocked = summary.get("recent_blocked", [])
                except Exception as exc:
                    _logger.debug(
                        "Failed to fetch audit summary for safety status: %s", exc
                    )

                # G-code blocked command list
                from kiln.gcode import _BLOCKED_COMMANDS  # noqa: E402

                blocked_gcode_commands = sorted(_BLOCKED_COMMANDS.keys())

                return {
                    "success": True,
                    "safety_profile": profile_info,
                    "temperature_limits": {"max_hotend": max_tool, "max_bed": max_bed},
                    "rate_limits": rate_limits,
                    "confirm_level_tools": confirm_tools,
                    "auth": auth_info,
                    "confirm_mode_enabled": confirm_mode,
                    "blocked_gcode_commands": blocked_gcode_commands,
                    "recent_blocked_actions": recent_blocked,
                    "summary": (
                        f"Safety profile: {profile_info.get('display_name', _srv._PRINTER_MODEL or 'default')}. "
                        f"Temp limits: {max_tool}\u00b0C hotend / {max_bed}\u00b0C bed. "
                        f"{len(rate_limits)} rate-limited tools. "
                        f"{len(confirm_tools)} confirm-level tools. "
                        f"{len(recent_blocked)} blocked action(s) in last hour."
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in safety_status")
                return _srv._error_dict(
                    f"Unexpected error in safety_status: {exc}", code="INTERNAL_ERROR"
                )

        # ------------------------------------------------------------------
        # safety_settings
        # ------------------------------------------------------------------

        @mcp.tool()
        def safety_settings() -> dict:
            """Show current safety and auto-print settings.

            Displays whether auto-print is enabled for marketplace downloads
            and AI-generated models, along with guidance on how to change them.
            Call this early in a session to understand what safety protections
            are active.
            """
            return {
                "success": True,
                "auto_print_marketplace": {
                    "enabled": _srv._AUTO_PRINT_MARKETPLACE,
                    "env_var": "KILN_AUTO_PRINT_MARKETPLACE",
                    "risk_level": "moderate",
                    "description": (
                        "When enabled, marketplace models are auto-printed after "
                        "download+upload. When disabled (default), models are "
                        "uploaded but require explicit start_print call."
                    ),
                },
                "auto_print_generated": {
                    "enabled": _srv._AUTO_PRINT_GENERATED,
                    "env_var": "KILN_AUTO_PRINT_GENERATED",
                    "risk_level": "high",
                    "description": (
                        "When enabled, AI-generated models are auto-printed after "
                        "generation+validation+slicing+upload. When disabled "
                        "(default), models are uploaded but require explicit "
                        "start_print call. Higher risk than marketplace models."
                    ),
                },
                "recommendations": [
                    "Prefer downloading proven community models over generating new ones.",
                    "Always validate meshes before printing (validate_generated_mesh).",
                    "Review model dimensions against your printer's build volume.",
                    "Keep auto-print disabled unless you understand the risks.",
                    "AI model generation is experimental \u2014 generated geometry may "
                    "have thin walls, non-manifold faces, or impossible overhangs.",
                ],
                "how_to_change": (
                    "Set environment variables before starting the MCP server:\n"
                    "  export KILN_AUTO_PRINT_MARKETPLACE=true   # moderate risk\n"
                    "  export KILN_AUTO_PRINT_GENERATED=true     # higher risk\n"
                    "Or run 'kiln setup' to configure interactively."
                ),
            }

        # ------------------------------------------------------------------
        # list_safety_profiles
        # ------------------------------------------------------------------

        @mcp.tool()
        def list_safety_profiles() -> dict:
            """List all available printer safety profiles.

            Returns a list of profile IDs and display names from the bundled
            safety database.  Use with ``get_safety_profile`` to inspect limits
            for a specific printer, or ``validate_gcode_safe`` to validate
            commands against a printer's limits.
            """
            if err := _srv._check_auth("safety"):
                return err
            try:
                from kiln.safety_profiles import (
                    get_profile,
                    limits_provenance_note,
                    list_profiles,
                )

                ids = list_profiles()
                profiles = []
                for pid in ids:
                    try:
                        p = get_profile(pid)
                        profiles.append(
                            {
                                "id": p.id,
                                "display_name": p.display_name,
                                "max_hotend_temp": p.max_hotend_temp,
                                "max_bed_temp": p.max_bed_temp,
                                # A roster row quotes two limits, so it owes
                                # the same one-line answer to "whose numbers
                                # are these?" the full profile carries — an
                                # owner-typed profile must not read like a
                                # curated one here either.
                                "limits_provenance": limits_provenance_note(p),
                            }
                        )
                    except KeyError:
                        continue
                return {"success": True, "count": len(profiles), "profiles": profiles}
            except Exception as exc:
                _logger.exception("Unexpected error in list_safety_profiles")
                return _srv._error_dict(
                    f"Unexpected error in list_safety_profiles: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # get_safety_profile
        # ------------------------------------------------------------------

        @mcp.tool()
        def get_safety_profile(printer_id: str) -> dict:
            """Get the full safety profile for a specific printer model.

            Returns temperature limits, feedrate limits, volumetric flow,
            build volume, and safety notes.  Falls back to the default
            profile if the printer_id is not found.

            The profile also says where its numbers came from:
            ``owner_supplied`` lists any limit fields whose values were
            typed by this machine's owner rather than verified by Kiln,
            and ``limits_provenance`` is a ready-made sentence stating
            it.  Repeat that sentence when quoting a limit, so a
            verified number and a typed one are never presented with
            the same authority.

            Args:
                printer_id: Printer model identifier (e.g. ``"ender3"``,
                    ``"bambu_x1c"``, ``"prusa_mk4"``).
            """
            if err := _srv._check_auth("safety"):
                return err
            try:
                from kiln.safety_profiles import get_profile, profile_to_dict

                profile = get_profile(printer_id)
                return {"success": True, "profile": profile_to_dict(profile)}
            except KeyError:
                return _srv._error_dict(
                    f"No safety profile for '{printer_id}' and no default available.",
                    code="NOT_FOUND",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in get_safety_profile")
                return _srv._error_dict(
                    f"Unexpected error in get_safety_profile: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # add_safety_profile
        # ------------------------------------------------------------------

        @mcp.tool()
        @_srv.requires_tier(_srv.LicenseTier.BUSINESS)
        def add_safety_profile(printer_model: str, profile: dict) -> dict:
            """Add a local safety-profile override for a printer model.

            Validates the profile and saves it to this machine's override file
            (``~/.kiln/local_printer_overrides.json``; the older name
            ``community_profiles.json`` is still read).  Nothing saved here is
            uploaded, pooled or shared.

            An override may only TIGHTEN a curated limit.  A higher number is
            discarded in favour of Kiln's curated value, so this is the right
            tool for a printer Kiln has never heard of, or for holding your own
            machine BELOW the curated limits.

            It is the WRONG tool for "my hotend is upgraded".  Use
            ``select_printer_variant`` for that: it resolves to a ceiling Kiln
            has verified against the manufacturer, instead of one you typed.

            Values saved here are labelled owner-supplied in every profile
            readout — Kiln never presents them as its own verified numbers.

            Args:
                printer_model: Short identifier for the printer (e.g.
                    ``"my_custom_corexy"``).
                profile: Dict containing at least ``max_hotend_temp``,
                    ``max_bed_temp``, ``max_feedrate``, and ``build_volume``
                    (a list of 3 positive numbers ``[X, Y, Z]``).  Optional
                    fields: ``display_name``, ``max_chamber_temp``, ``min_safe_z``,
                    ``max_volumetric_flow``, ``notes``.
            """
            if err := _srv._check_auth("safety"):
                return err
            try:
                from kiln.safety_profiles import (
                    add_community_profile,
                    validate_safety_profile,
                )

                errors = validate_safety_profile(profile)
                if errors:
                    return _srv._error_dict(
                        f"Validation failed: {'; '.join(errors)}",
                        code="VALIDATION_ERROR",
                    )
                add_community_profile(printer_model, profile)
                return {
                    "success": True,
                    "printer_model": printer_model.lower().replace("-", "_").strip(),
                    "message": "Local printer override saved successfully.",
                }
            except ValueError as exc:
                return _srv._error_dict(
                    f"Failed to add safety profile: {exc}", code="VALIDATION_ERROR"
                )
            except Exception as exc:
                _logger.exception("Unexpected error in add_safety_profile")
                return _srv._error_dict(
                    f"Unexpected error in add_safety_profile: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # list_printer_variants / select_printer_variant
        # ------------------------------------------------------------------

        @mcp.tool()
        def list_printer_variants(printer_model: str) -> dict:
            """Show the curated hardware variants available for a printer.

            A curated profile describes a printer AS SHIPPED.  When Kiln has
            verified a documented hardware change — an Ender 3 whose PTFE-lined
            hotend has been replaced with an E3D Revo CR, say — that
            configuration is curated as a VARIANT, with its own limits, its
            manufacturer source, and the preconditions that make it true.

            Returns the as-shipped limits alongside each variant's, so you can
            see what selecting one would change before selecting it.  An empty
            ``variants`` map is the honest answer for a machine Kiln has not
            verified a modified configuration for.

            Args:
                printer_model: Printer identifier (e.g. ``"ender3"``).
            """
            if err := _srv._check_auth("safety"):
                return err
            try:
                from kiln.safety_profiles import list_printer_variants as _list

                return {"success": True, **_list(printer_model)}
            except Exception as exc:
                _logger.exception("Unexpected error in list_printer_variants")
                return _srv._error_dict(
                    f"Unexpected error in list_printer_variants: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def select_printer_variant(printer_model: str, variant_id: str = "") -> dict:
            """Declare which curated hardware variant your machine actually is.

            This is how an operator with a modified printer gets an accurate
            ceiling WITHOUT typing one.  You say which hardware you have; Kiln
            supplies the limit from curated, manufacturer-sourced data.  There
            is no argument here that accepts a temperature, which is the point:
            a limit Kiln enforces is always a limit Kiln verified.

            Check ``requires`` on the variant first — a ceiling is only true if
            its preconditions are met.  Several variants need a firmware change
            as well as the part, and selecting the variant is your statement
            that you have done both.  Kiln cannot check your hardware remotely.

            The declaration stays on this machine.  It is never uploaded or
            pooled, and it is ignored entirely on hosted multi-tenant
            deployments, where "this machine" has no single owner.

            Args:
                printer_model: Printer identifier (e.g. ``"ender3"``).
                variant_id: Variant to declare, from ``list_printer_variants``.
                    Pass ``""`` to go back to the as-shipped profile.
            """
            if err := _srv._check_auth("safety"):
                return err
            try:
                from kiln.safety_profiles import list_printer_variants as _list
                from kiln.safety_profiles import select_printer_variant as _select

                result = _select(printer_model, variant_id or None)
                if result.get("variant"):
                    spec = _list(printer_model)["variants"].get(result["variant"], {})
                    result["requires"] = spec.get("requires", [])
                return {"success": True, **result}
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="VALIDATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in select_printer_variant")
                return _srv._error_dict(
                    f"Unexpected error in select_printer_variant: {exc}",
                    code="INTERNAL_ERROR",
                )


plugin = _SafetyToolsPlugin()
