"""Utility and read-only tools plugin.

Extracts utility and system-health MCP tools from server.py into a focused
plugin module.  All tools delegate to lazy-imported singletons from
:mod:`kiln.server`.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

_logger = logging.getLogger(__name__)


class _UtilityToolsPlugin:
    """Utility, health-check, onboarding, and admin tools.

    Tools:
        - get_session_log
        - health_check
        - kiln_health
        - get_started
        - get_skill_manifest
        - verify_audit_integrity
        - backup_database
        - plugin_info
    """

    @property
    def name(self) -> str:
        return "utility_tools"

    @property
    def description(self) -> str:
        return "Utility, health-check, onboarding, and admin tools"

    def register(self, mcp: Any) -> None:  # noqa: PLR0915
        """Register utility tools with the MCP server."""

        # ------------------------------------------------------------------
        # get_session_log
        # ------------------------------------------------------------------

        @mcp.tool()
        def get_session_log(
            session_id: str | None = None,
            limit: int = 100,
        ) -> dict:
            """Return the full audit log for an agent session.

            Every tool call made by an agent is recorded with a session ID — a UUID
            generated when the MCP server starts.  Use this tool to replay exactly
            what an agent issued during a session: every command, every safety check
            that fired, every blocked attempt.

            Args:
                session_id: Session UUID to query.  Omit to use the current session.
                limit: Maximum records to return (default 100, max 500).
            """
            import kiln.server as _srv
            from kiln.persistence import get_db

            limit = min(max(1, limit), 500)
            sid = session_id or _srv._SESSION_ID
            try:
                db = get_db()
                entries = db.query_audit(session_id=sid, limit=limit)
                return {
                    "success": True,
                    "session_id": sid,
                    "current_session": sid == _srv._SESSION_ID,
                    "count": len(entries),
                    "entries": entries,
                }
            except Exception as exc:
                _logger.exception("Unexpected error in get_session_log")
                return _srv._error_dict(
                    f"Unexpected error in get_session_log: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # health_check
        # ------------------------------------------------------------------

        @mcp.tool()
        def health_check() -> dict:
            """Return system health information for monitoring.

            No authentication required.  Useful for container healthchecks,
            dashboards, and verifying the server is responsive.

            **See also:** ``kiln_health`` for version info, module
            availability, scheduler status, and webhook configuration.
            """
            import platform

            import kiln.server as _srv
            from kiln.persistence import get_db

            uptime_s = time.time() - _srv._start_time
            hours = int(uptime_s // 3600)
            minutes = int((uptime_s % 3600) // 60)
            secs = int(uptime_s % 60)

            db_ok = False
            try:
                get_db()._conn.execute("SELECT 1")
                db_ok = True
            except Exception as exc:
                _logger.debug("Database health check failed: %s", exc)

            health_data: dict[str, Any] = {
                "success": True,
                "status": "healthy",
                "uptime": f"{hours}h {minutes}m {secs}s",
                "uptime_seconds": round(uptime_s, 1),
                "printers_registered": _srv._get_registry().count,
                "queue_pending": _srv._get_queue().pending_count(),
                "queue_active": _srv._get_queue().active_count(),
                "queue_total": _srv._get_queue().total_count,
                "scheduler_running": _srv._get_scheduler().is_running,
                "database_reachable": db_ok,
                "python_version": platform.python_version(),
                "platform": platform.system(),
                "auth_enabled": os.environ.get("KILN_AUTH_ENABLED", "").lower()
                in ("1", "true", "yes"),
            }

            try:
                alert_mgr = _srv._get_billing_alert_mgr()
                health_data["billing_health"] = alert_mgr.get_health_summary()
            except Exception as exc:
                _logger.debug("Failed to get billing health summary: %s", exc)
                health_data["billing_health"] = {"status": "unknown"}

            try:
                from kiln.emboss_generator import (
                    _OPENSCAD_MIN_VERSION_YEAR,
                    _OPENSCAD_UPGRADE_MSG,
                    _openscad_version_year,
                    get_openscad_version,
                )

                _platform = sys.platform
                if _platform == "darwin":
                    _install_cmd = "brew install --cask openscad@snapshot"
                elif _platform.startswith("linux"):
                    _install_cmd = "sudo snap install openscad --edge"
                else:
                    _install_cmd = "Download from https://openscad.org/downloads#snapshots"

                openscad_version = get_openscad_version()
                openscad_info: dict[str, Any] = {"version": openscad_version or "not_found"}
                if openscad_version:
                    year = _openscad_version_year(openscad_version)
                    if year and year < _OPENSCAD_MIN_VERSION_YEAR:
                        openscad_info["warning"] = _OPENSCAD_UPGRADE_MSG
                        openscad_info["svg_operations_supported"] = False
                        openscad_info["install_command"] = _install_cmd
                    else:
                        openscad_info["svg_operations_supported"] = True
                else:
                    openscad_info["install_command"] = _install_cmd
                health_data["openscad"] = openscad_info
            except Exception as exc:
                _logger.debug("Failed to get OpenSCAD version: %s", exc)
                health_data["openscad"] = {"version": "unknown"}

            return health_data

        # ------------------------------------------------------------------
        # kiln_health
        # ------------------------------------------------------------------

        @mcp.tool()
        def kiln_health() -> dict:
            """Get a health check for the Kiln system.

            Returns versions, uptime, module availability, scheduler status,
            webhook status, and overall system health.  Use this to verify the
            system is running correctly.
            """
            import kiln
            import kiln.server as _srv

            uptime_secs = time.time() - _srv._start_time
            hours, rem = divmod(int(uptime_secs), 3600)
            mins, secs = divmod(rem, 60)

            modules = {
                "scheduler": _srv._get_scheduler().is_running,
                "webhooks": _srv._get_webhook_mgr().is_running,
                "persistence": True,
                "auth_enabled": _srv._get_auth().enabled,
                "billing": True,
                "thingiverse": bool(_srv._THINGIVERSE_TOKEN),
            }

            try:
                import kiln.printers.bambu  # noqa: F401 -- availability check only

                modules["bambu_available"] = True
            except ImportError:
                modules["bambu_available"] = False

            # Surface the active safety profile so users can see whether
            # the safety stack is running with full gates or soft-passing.
            # Incident #0 follow-up: previously users had no visibility
            # into whether _PRINTER_MODEL was set, which determined half
            # the safety gates.
            safety_profile_info: dict[str, Any] = {
                "resolved_model": None,
                "resolution_source": "unknown",
                "gates_active": False,
                "hint": None,
            }
            try:
                from kiln.printer_model_resolver import resolve_printer_model_with_source
                _model, _source = resolve_printer_model_with_source()
                safety_profile_info["resolved_model"] = _model
                safety_profile_info["resolution_source"] = _source
                safety_profile_info["gates_active"] = _model is not None
                if _model is None:
                    safety_profile_info["hint"] = (
                        "Set `printer_model: <your-model>` in ~/.kiln/config.yaml "
                        "to activate bed-fit, bounds, and temperature safety gates. "
                        "See kiln/data/printer_intelligence.json for valid keys."
                    )
                elif _source in ("type_fallback", "serial_inference", "host_pattern"):
                    safety_profile_info["hint"] = (
                        f"Model inferred via {_source}; set `printer_model: {_model}` "
                        f"in ~/.kiln/config.yaml to make it explicit."
                    )
            except Exception as exc:
                safety_profile_info["hint"] = f"resolver error: {exc}"

            return {
                "success": True,
                "version": kiln.__version__,
                "uptime_seconds": int(uptime_secs),
                "uptime_human": f"{hours}h {mins}m {secs}s",
                "printers_registered": _srv._get_registry().count,
                "queue_depth": _srv._get_queue().total_count,
                "scheduler_running": _srv._get_scheduler().is_running,
                "webhook_endpoints": len(_srv._get_webhook_mgr().list_endpoints()),
                "modules": modules,
                "safety_profile": safety_profile_info,
                "healthy": True,
            }

        # ------------------------------------------------------------------
        # get_started
        # ------------------------------------------------------------------

        @mcp.tool()
        def get_started() -> dict:
            """Quick-start guide for AI agents using Kiln.

            Returns an onboarding summary: what Kiln is, how to discover
            its tools, core workflows, and the most useful tools to call
            first.  Call this at the start of a session if you're
            unfamiliar with the available capabilities.
            """
            import kiln.server as _srv

            # Live tool count — authoritative number of MCP tools this
            # session can actually call (public Kiln + kiln-pro if installed
            # + any manifest stubs).  Prior versions of this tool returned
            # stale static tier buckets that didn't account for kiln-pro.
            try:
                live_tool_count = len(_srv.mcp._tool_manager.list_tools())
            except Exception:
                live_tool_count = 0

            # Detect whether kiln-pro is providing real tools (not just stubs)
            kiln_pro_installed = False
            try:
                import importlib
                importlib.import_module("kiln_pro")
                kiln_pro_installed = True
            except ImportError:
                pass

            # Check OpenSCAD installation status for guidance
            openscad_guidance: dict[str, Any] = {}
            _openscad_action_needed = False
            try:
                from kiln.emboss_generator import _openscad_version_year, get_openscad_version

                _platform = sys.platform
                if _platform == "darwin":
                    _install_cmd = "brew install --cask openscad@snapshot"
                elif _platform.startswith("linux"):
                    _install_cmd = "sudo snap install openscad --edge"
                else:
                    _install_cmd = "Download from https://openscad.org/downloads#snapshots"

                _ver = get_openscad_version()
                if not _ver:
                    _openscad_action_needed = True
                    openscad_guidance = {
                        "installed": False,
                        "message": (
                            "Install OpenSCAD for 3D model generation and decoration: "
                            "brew install --cask openscad@snapshot  (macOS) "
                            "or download from https://openscad.org/downloads"
                        ),
                        "install_command": _install_cmd,
                        "required_for": [
                            "compile_scad",
                            "generate_product_base",
                            "decorate_surface",
                            "visualize_model",
                        ],
                    }
                elif _openscad_version_year(_ver) < 2024:
                    _openscad_action_needed = True
                    openscad_guidance = {
                        "installed": True,
                        "version": _ver,
                        "status": "outdated",
                        "message": (
                            f"OpenSCAD {_ver} is outdated. Upgrade for full feature support: "
                            "brew install --cask openscad@snapshot  (macOS) "
                            "or download from https://openscad.org/downloads"
                        ),
                        "install_command": _install_cmd,
                        "required_for": [
                            "compile_scad",
                            "generate_product_base",
                            "decorate_surface",
                            "visualize_model",
                        ],
                    }
                else:
                    openscad_guidance = {"installed": True, "version": _ver, "status": "ok"}
            except Exception:  # noqa: BLE001
                openscad_guidance = {"installed": False, "status": "unknown"}

            _quick_start_base = [
                "1. Call `printer_status` to check if a printer is connected and its current state.",
                "2. Call `fleet_status` if managing multiple printers.",
                "3. Call `preflight_check` before starting any print to validate readiness.",
                "4. Use `search_all_models` to find 3D models across marketplaces.",
                "5. Use `slice_model` or `slice_and_print` to prepare and print files.",
                "6. Use `validate_gcode` before `send_gcode` for raw G-code commands.",
            ]
            if _openscad_action_needed:
                _openscad_step = (
                    f"0. IMPORTANT: {openscad_guidance.get('message', 'Install/upgrade OpenSCAD')} "
                    f"— run: {openscad_guidance.get('install_command', '')}"
                )
                _quick_start = [_openscad_step] + _quick_start_base
            else:
                _quick_start = _quick_start_base

            return {
                "success": True,
                "overview": (
                    f"Kiln is agent infrastructure for 3D printing. This session "
                    f"has {live_tool_count} MCP tools covering printer monitoring, "
                    f"file management, slicing, marketplaces, model generation, "
                    f"design intelligence, safety controls"
                    + (
                        ", and kiln-pro features (product generators, decoration, "
                        "print intelligence, fleet ops, billing)."
                        if kiln_pro_installed
                        else ". Some tools you discover may be Pro/Business-tier "
                        "stubs — their descriptions include tier + upgrade URL "
                        "so you can surface upgrade messaging to the user."
                    )
                ),
                "tool_discovery": {
                    "total_tools": live_tool_count,
                    "how_to_discover": (
                        "MCP clients don't load all tool schemas upfront when "
                        "the count is large — use ToolSearch(keyword) to surface "
                        "relevant schemas on demand. Examples: ToolSearch('slice "
                        "bambu'), ToolSearch('ams filament'), ToolSearch('billing'), "
                        "ToolSearch('generate coaster')."
                    ),
                    "capability_map": (
                        "Call get_skill_manifest() for the full machine-readable "
                        "capability map: agent rules, common workflows, tool "
                        "recommendations by use case, and tier system."
                    ),
                    "tier_visibility": (
                        "Pro/Business tools appear in ToolSearch results with "
                        "'Requires Kiln Pro/Business' and an upgrade URL in the "
                        "description. Free-tier agents can surface them to users "
                        "for upgrade messaging even when kiln-pro isn't locally "
                        "installed."
                    ),
                    "kiln_pro_installed": kiln_pro_installed,
                },
                "quick_start": _quick_start,
                "core_workflows": {
                    "print_a_file": "upload_file → visualize_model → preflight_check → start_print",
                    "marketplace_to_print": (
                        "search_all_models → download_and_upload → preflight_check → start_print"
                    ),
                    "slice_and_print": "upload_file (STL) → slice_and_print",
                    "monitor": "printer_status, printer_snapshot, await_print_completion",
                    "queue_jobs": "submit_job → job_status → queue_summary",
                },
                "safety_tools": [
                    "preflight_check — validates printer readiness before printing",
                    "validate_gcode — checks G-code for dangerous commands before sending",
                    "safety_status — comprehensive safety dashboard (limits, rate-limits, blocked actions, auth)",
                    "safety_settings — shows current auto-print and confirmation settings",
                    "safety_audit — reviews recent safety-relevant actions",
                ],
                "session_recovery": {
                    "description": (
                        "If resuming a previous session, call get_agent_context to restore your memory."
                    ),
                    "tool": "get_agent_context",
                    "usage": (
                        "Call get_agent_context() at session start to retrieve "
                        "notes saved in prior sessions."
                    ),
                },
                "tip": (
                    "Start with `printer_status` to see what's connected. "
                    "Check `safety_status` / `safety_settings` for guardrails "
                    "and auto-print state. Use ToolSearch(keyword) to discover "
                    "tools for specific tasks rather than guessing names, or "
                    "get_skill_manifest() for the full capability map."
                ),
                "openscad": openscad_guidance,
            }

        # ------------------------------------------------------------------
        # get_skill_manifest
        # ------------------------------------------------------------------

        @mcp.tool()
        def get_skill_manifest() -> dict:
            """Get the Kiln skill manifest for agent self-discovery.

            Returns a machine-readable description of Kiln's capabilities,
            configuration requirements, available interfaces, and setup
            instructions.  Use this when first connecting to understand what
            Kiln can do and what configuration is needed.
            """
            import kiln.server as _srv

            try:
                from kiln.skill_manifest import generate_manifest

                manifest = generate_manifest()
                return {"success": True, "data": manifest.to_dict()}
            except Exception as exc:
                _logger.exception("Unexpected error in get_skill_manifest")
                return _srv._error_dict(
                    f"Failed to generate manifest: {exc}", code="INTERNAL_ERROR"
                )

        # ------------------------------------------------------------------
        # verify_audit_integrity
        # ------------------------------------------------------------------

        @mcp.tool()
        def verify_audit_integrity() -> dict:
            """Verify HMAC signatures on all safety audit log entries.

            Checks each audit log row against its stored HMAC signature to
            detect tampering.  Returns counts of valid, invalid, and total
            entries along with an overall integrity status.
            """
            import kiln.server as _srv
            from kiln.persistence import get_db

            auth_err = _srv._check_auth("admin")
            if auth_err:
                return auth_err
            try:
                db = get_db()
                result = db.verify_audit_log()
                return {
                    "success": True,
                    **result,
                }
            except Exception as exc:
                _logger.exception("Unexpected error in verify_audit_integrity")
                return _srv._error_dict(
                    f"Unexpected error in verify_audit_integrity: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # backup_database
        # ------------------------------------------------------------------

        @mcp.tool()
        def backup_database(
            output_path: str | None = None,
            redact: bool = True,
        ) -> dict:
            """Back up the Kiln database with optional credential redaction.

            Creates a copy of the SQLite database.  By default, sensitive fields
            (API keys, access codes, payment refs) are replaced with "REDACTED"
            in the backup.

            Args:
                output_path: Destination file path.  Defaults to
                    ``~/.kiln/backups/kiln-YYYYMMDD-HHMMSS.db``.
                redact: If ``True`` (default), redact credentials in the backup.
            """
            import kiln.server as _srv
            from kiln.backup import BackupError
            from kiln.backup import backup_database as _backup_db
            from kiln.persistence import get_db

            auth_err = _srv._check_auth("admin")
            if auth_err:
                return auth_err
            try:
                db = get_db()
                result_path = _backup_db(
                    db.path,
                    output_path,
                    redact_credentials=redact,
                )
                return {
                    "success": True,
                    "backup_path": result_path,
                    "redacted": redact,
                }
            except BackupError as exc:
                return _srv._error_dict(
                    f"Failed to back up database: {exc}", code="BACKUP_ERROR"
                )
            except Exception as exc:
                _logger.exception("Unexpected error in backup_database")
                return _srv._error_dict(
                    f"Unexpected error in backup_database: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # plugin_info
        # ------------------------------------------------------------------

        @mcp.tool()
        def plugin_info(name: str) -> dict:
            """Get detailed information about a specific plugin.

            Args:
                name: Plugin name.
            """
            import kiln.server as _srv

            info = _srv._get_plugin_mgr().get_plugin_info(name)
            if info is None:
                return _srv._error_dict(f"Plugin {name!r} not found.", code="NOT_FOUND")
            return {"success": True, "plugin": info.to_dict()}

        _logger.debug("Registered utility tools")


plugin = _UtilityToolsPlugin()
