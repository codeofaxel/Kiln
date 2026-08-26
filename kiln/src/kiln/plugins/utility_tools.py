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
import time
from typing import Any

_logger = logging.getLogger(__name__)

def _template_part_count() -> int:
    """How many parametric parts this build can render, counted live.

    A number in onboarding copy is a number that goes stale — the skill
    manifest advertised "18 templates" for a 65-part library for exactly
    that reason.
    """
    try:
        from kiln.design_intelligence import list_generatable_design_templates

        return len(list_generatable_design_templates())
    except Exception:  # noqa: BLE001
        return 0



class _UtilityToolsPlugin:
    """Utility, health-check, onboarding, and admin tools.

    Tools:
        - get_session_log
        - upgrade_kiln
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
        # upgrade_kiln
        # ------------------------------------------------------------------

        @mcp.tool()
        def upgrade_kiln(confirm: bool = False, force: bool = False) -> dict:
            """Update the Kiln package to the latest version — for the user.

            The Apple-grade upgrade path. When a newer Kiln is available (or a
            hosted call returns an upgrade-required signal), OFFER to handle it:
            ask "want me to update Kiln for you now?" and call this with
            confirm=True once they agree. Don't make the user run a pip command.

            AGENT CONTRACT (important):
              * NEVER call this while a print is active — wait until it finishes.
                Swapping Kiln mid-print is unsafe.
              * Confirm with the user first; this changes their installed
                software. Pass confirm=True only after they say yes.
              * On success the new version is on disk but the running Kiln still
                has the old code loaded — relay the restart instruction from the
                result so the user applies it at a safe moment (not mid-print).

            Args:
                confirm: Set True to actually perform the update. Called without
                    it, this returns the offer to show the user and changes
                    nothing.
                force: Override the mid-print safety defer — only when the user
                    explicitly insists.
            """
            import kiln.server as _srv

            try:
                from kiln import self_update
            except Exception as exc:  # noqa: BLE001
                return _srv._error_dict(
                    f"Upgrade is unavailable in this build: {exc}",
                    code="INTERNAL_ERROR",
                )

            current = self_update.current_version()
            if not confirm:
                return {
                    "success": True,
                    "status": "needs_confirmation",
                    "current": current,
                    "message": (
                        "Want me to update Kiln for you now? It takes a few "
                        "seconds, then one quick restart at a safe moment (not "
                        "mid-print) and I'll pick up right where we left off. "
                        "Confirm and I'll run it (upgrade_kiln with confirm=true)."
                    ),
                }

            # The funnel's second half.  The offer being SHOWN is
            # recorded where it is attached (kiln.update_nudge); this
            # records what the user's machine did about it.  Without the
            # split, "nobody was asked" and "everybody was asked and pip
            # failed" are the same zero — different products, different
            # fixes.  Best-effort throughout: telemetry never turns an
            # upgrade into an error.
            def _stage(name: str) -> None:
                try:
                    from kiln.daily_stats import record_update_nudge

                    record_update_nudge(name)
                except Exception:  # noqa: BLE001
                    pass

            _stage("upgrade_attempted")
            try:
                result = self_update.perform_upgrade(force=force)
            except Exception as exc:  # noqa: BLE001 -- never surface a raw traceback
                _logger.exception("Unexpected error in upgrade_kiln")
                _stage("upgrade_failed")
                return _srv._error_dict(
                    f"Update failed unexpectedly: {exc}. You can run it yourself: "
                    f"{self_update.UPGRADE_COMMAND}",
                    code="INTERNAL_ERROR",
                )
            # A mid-print defer is neither a yes nor a no — the user was
            # never asked to choose, so counting it as a failure would
            # read as "upgrades are broken" when the safety rule worked.
            if result.get("status") == "deferred_active_print":
                _stage("upgrade_deferred")
            elif result.get("ok"):
                _stage("upgrade_ok")
            else:
                _stage("upgrade_failed")
            return {"success": bool(result.get("ok")), **result}

        # ------------------------------------------------------------------
        # trim_serve_processes
        # ------------------------------------------------------------------

        @mcp.tool()
        def trim_serve_processes(
            confirm: bool = False,
            open_sessions: int | None = None,
            force: bool = False,
        ) -> dict:
            """Close leftover Kiln servers left behind by closed sessions.

            Every agent session spawns its own background Kiln server;
            client apps don't reliably close them when a session ends, so
            they accumulate and quietly hold memory. This is the cleanup
            the pile-up warning (health_check / kiln_health / get_started)
            offers.

            WHY THIS IS SAFE: closing a server never stops a physical
            print — the printer keeps going regardless. What a close can
            end is MONITORING (a running watch loop). So this refuses to
            act while any printer has a job in flight, and otherwise the
            worst case is that a still-open session reconnects. This
            session's own server is never closed.

            AGENT CONTRACT (important):
              * ASK THE USER how many agent sessions they actually have
                open right now and pass it as open_sessions — they are
                the one source of truth for that, and it beats guessing
                from process age. Never ask them for a PID.
              * Confirm before acting. Called without confirm, this
                returns the plan (how many would close, how many stay)
                to show the user; pass confirm=True once they agree.
              * If it comes back blocked because something is printing,
                tell the user what's printing and leave it alone — offer
                to clean up after the print finishes rather than
                reaching for force.

            Args:
                confirm: Set True to actually close the leftovers.
                open_sessions: The user's own count of agent sessions
                    currently open. Keeps that many most-recently-started
                    servers and proposes the rest.
                force: Proceed even though a printer has a job in flight.
                    Only when the user explicitly insists, knowing that
                    monitoring for that job may stop.
            """
            from kiln.serve_siblings import perform_trim, plan_trim, printing_now

            if not confirm:
                plan = plan_trim(open_sessions=open_sessions)
                if plan["scanned"] is None:
                    return {
                        "success": False,
                        "status": "unavailable",
                        "message": "Cannot read the process table on this platform.",
                    }
                printing = printing_now()
                if not plan["candidates"]:
                    return {
                        "success": True,
                        "status": "nothing_to_trim",
                        "plan": plan,
                        "message": (
                            f"All {plan['scanned']} running Kiln server(s) look "
                            f"current — nothing to clean up."
                        ),
                    }
                if printing["active"]:
                    return {
                        "success": True,
                        "status": "blocked_printing",
                        "plan": plan,
                        "printing": printing,
                        "message": (
                            f"{', '.join(printing['active'])} — a print is in "
                            f"progress, so closing servers now could stop the "
                            f"monitoring for it. {len(plan['candidates'])} "
                            f"leftover server(s) are waiting; offer to clean "
                            f"them up once the print finishes."
                        ),
                    }
                return {
                    "success": True,
                    "status": "needs_confirmation",
                    "plan": plan,
                    "printing": printing,
                    "message": (
                        f"{len(plan['candidates'])} leftover Kiln server(s) can "
                        f"be closed ({len(plan['kept'])} stay, including this "
                        f"session's). Nothing is printing, so the worst case is "
                        f"that a still-open session reconnects. Want me to close "
                        f"them? Call again with confirm=true once the user says "
                        f"yes."
                    ),
                }

            result = perform_trim(open_sessions=open_sessions, force=force)
            if result["blocked"]:
                return {
                    "success": False,
                    "status": "blocked_printing",
                    **result,
                    "message": (
                        f"Not closing anything: {', '.join(result['printing']['active'])} "
                        f"— a print is in progress and monitoring could be lost. "
                        f"Try again once it finishes."
                    ),
                }
            return {
                "success": not result["failed"],
                "status": "trimmed",
                **result,
                "message": (
                    f"Closed {len(result['trimmed'])} leftover server(s); "
                    f"{len(result['kept'])} kept (including this session's)."
                    + (f" {len(result['failed'])} could not be closed." if result["failed"] else "")
                ),
            }

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

            # Sibling serve processes — accumulated servers from closed
            # sessions are the one health problem the server can't see
            # from the inside.  Shared detector: kiln.serve_siblings.
            try:
                from kiln.serve_siblings import check_serve_siblings

                health_data["serve_processes"] = check_serve_siblings()
            except Exception as exc:
                _logger.debug("Serve-sibling check failed: %s", exc)
                health_data["serve_processes"] = {"count": None, "warning": None}

            # And whether those servers are using up the printer's connection
            # slots — the same pile-up, measured where it actually bites.
            try:
                from kiln.serve_siblings import printer_slot_report

                health_data["printer_connections"] = printer_slot_report()
            except Exception as exc:
                _logger.debug("Printer-slot check failed: %s", exc)
                health_data["printer_connections"] = {
                    "checked": False, "hosts": [], "warning": None,
                }

            try:
                from kiln.emboss_generator import (
                    _OPENSCAD_MIN_VERSION_YEAR,
                    _OPENSCAD_UPGRADE_MSG,
                    _openscad_install_command,
                    _openscad_version_year,
                    get_openscad_version,
                )

                _install_cmd = _openscad_install_command()

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

            # Surface the active safety profile so users + agents can
            # see whether the safety stack is running with full gates
            # or soft-passing.  Single source of truth: the
            # `printer_model` field in ~/.kiln/config.yaml.  If absent,
            # the agent should ask the user and call `set_printer_model`.
            safety_profile_info: dict[str, Any] = {
                "printer_model": None,
                "gates_active": False,
                "hint": None,
            }
            try:
                from kiln.printer_model_resolver import resolve_printer_model
                _model = resolve_printer_model()
                safety_profile_info["printer_model"] = _model
                safety_profile_info["gates_active"] = _model is not None
                if _model is None:
                    safety_profile_info["hint"] = (
                        "printer_model is NOT configured, so Kiln can't check "
                        "that prints fit the bed or stay within safe temperatures "
                        "— those checks are skipped and an unsafe print can reach "
                        "the printer. Ask the user which printer they're using, "
                        "then add `printer_model: <model>` under "
                        "`printers.<name>` in ~/.kiln/config.yaml (examples: "
                        "bambu_a1, bambu_x1c, prusa_mk4, prusa_mini, ender3, "
                        "creality_k1_max). "
                        "Valid keys are in kiln/data/printer_intelligence.json."
                    )
            except Exception as exc:
                safety_profile_info["hint"] = f"resolver error: {exc}"

            # Non-blocking update nudge — cached PyPI check, None when current.
            try:
                from kiln.version_check import check_for_update

                update_info = check_for_update()
            except Exception:  # noqa: BLE001
                update_info = None

            # Sibling serve processes — shared detector, see
            # kiln.serve_siblings.  When `warning` is set, relay it to
            # the user verbatim; the user decides what to trim.
            try:
                from kiln.serve_siblings import check_serve_siblings

                serve_processes = check_serve_siblings()
            except Exception:  # noqa: BLE001
                serve_processes = {"count": None, "warning": None}

            # Printer connection slots — whether this machine's own servers
            # are using them up.  Reported here and not only in `kiln doctor`
            # because an agent meets this as "the printer is unreachable" and
            # has no other way to tell that from a powered-off machine; the
            # terminal check is the door the user knocks on deliberately, this
            # is the one they arrive at by accident.  Scans only when a
            # slot-rationing printer is configured.
            try:
                from kiln.serve_siblings import printer_slot_report

                printer_connections = printer_slot_report()
            except Exception:  # noqa: BLE001
                printer_connections = {"checked": False, "hosts": [], "warning": None}

            return {
                "success": True,
                "version": kiln.__version__,
                "update": update_info,
                "uptime_seconds": int(uptime_secs),
                "uptime_human": f"{hours}h {mins}m {secs}s",
                "printers_registered": _srv._get_registry().count,
                "queue_depth": _srv._get_queue().total_count,
                "scheduler_running": _srv._get_scheduler().is_running,
                "webhook_endpoints": len(_srv._get_webhook_mgr().list_endpoints()),
                "modules": modules,
                "safety_profile": safety_profile_info,
                "serve_processes": serve_processes,
                "printer_connections": printer_connections,
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
            try:
                live_prompt_count = len(_srv.mcp._prompt_manager.list_prompts())
            except Exception:
                live_prompt_count = 0
            try:
                live_resource_count = len(_srv.mcp._resource_manager.list_resources())
            except Exception:
                live_resource_count = 0
            live_capability_count = live_tool_count + live_prompt_count + live_resource_count

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
                from kiln.emboss_generator import (
                    _openscad_install_command,
                    _openscad_version_year,
                    get_openscad_version,
                )

                _install_cmd = _openscad_install_command()

                _ver = get_openscad_version()
                if not _ver:
                    _openscad_action_needed = True
                    openscad_guidance = {
                        "installed": False,
                        "message": (
                            "Install OpenSCAD — Kiln's design engine — to make and "
                            f"decorate models: {_install_cmd}"
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
                            f"OpenSCAD {_ver} is outdated — it is slower and silently "
                            f"fails SVG. Upgrade: {_install_cmd}"
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
                "2. `printer_status` takes a printer name — use it per machine "
                "when you have more than one (managing them together is a "
                "Business feature).",
                "3. Call `preflight_check` before starting any print to validate readiness.",
                "4. Use `search_all_models` to find 3D models across marketplaces.",
                "4b. Making a functional part to the user's own dimensions? "
                "Call `find_design_templates(use_case)` first — Kiln ships a "
                "library of print-verified parametric parts that render free "
                "and locally via `generate_from_template`.",
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

            # Non-blocking update nudge — cached PyPI check, None when current.
            try:
                from kiln.version_check import check_for_update

                _update_info = check_for_update()
            except Exception:  # noqa: BLE001
                _update_info = None

            # Signed-in state, so the agent can offer a free account at the
            # natural moment (a save / share) rather than nagging. Reads the
            # same token file the `kiln signin` CLI writes.
            try:
                from kiln.cli.auth_commands import _read_tokens

                _tokens = _read_tokens()
            except Exception:  # noqa: BLE001
                _tokens = {}
            if _tokens.get("access_token"):
                _account = {"signed_in": True, "email": _tokens.get("email") or ""}
            else:
                from kiln.tiers_and_terms import AGENT_ACCOUNT_NUDGE

                _account = {
                    "signed_in": False,
                    "tool": "kiln_signin",
                    "message": AGENT_ACCOUNT_NUDGE,
                }

            # Serve-process pile-up nudge — get_started is the mandated
            # first call, so it's where an agent learns the machine has
            # accumulated servers from closed sessions.  Only included
            # when the shared detector actually warns; a healthy count
            # adds nothing to onboarding.
            _serve_pileup = None
            try:
                from kiln.serve_siblings import (
                    check_serve_siblings,
                    printer_slot_report,
                )

                _sibling_report = check_serve_siblings()
                # Slots first: a pile-up that is actively holding the printer
                # hostage is a different message from one that is only using
                # memory, and it can be true before the process count crosses
                # its own warning threshold — a Bambu runs out of connections
                # long before a laptop runs out of RAM.
                _slot_report = printer_slot_report()
                if _slot_report.get("warning"):
                    _serve_pileup = {
                        **_sibling_report,
                        "printer_connections": _slot_report,
                        "action": (
                            "Tell the user now, before trying the printer "
                            "again: leftover Kiln servers are holding this "
                            "printer's connection slots, which is why it may "
                            "time out or look offline while being powered on "
                            "and on the network. Offer trim_serve_processes "
                            "(ask how many agent sessions they have open and "
                            "pass that as open_sessions; never ask for a PID). "
                            "Do NOT suggest power-cycling the printer, and do "
                            "not blame Bambu Studio or the Handy app — this is "
                            "Kiln's own doing. No running print is at risk; the "
                            "tool refuses while anything is printing."
                        ),
                    }
                elif _sibling_report.get("warning"):
                    _serve_pileup = {
                        **_sibling_report,
                        "action": (
                            "Mention this to the user — they can't see it from "
                            "inside a session — and offer to clean it up with "
                            "trim_serve_processes. Ask how many agent sessions "
                            "they actually have open and pass that as "
                            "open_sessions; never ask them for a PID. It is not "
                            "urgent and no print is at risk (the tool refuses "
                            "while anything is printing), so raise it once, "
                            "lightly, and drop it if they aren't interested."
                        ),
                    }
            except Exception:  # noqa: BLE001
                _serve_pileup = None

            return {
                "success": True,
                "update": _update_info,
                "account": _account,
                **({"serve_process_pileup": _serve_pileup} if _serve_pileup else {}),
                "overview": (
                    f"Kiln is agent infrastructure for 3D printing. This session "
                    f"has {live_tool_count} MCP tools ({live_capability_count} "
                    f"total MCP capabilities including {live_prompt_count} prompts "
                    f"and {live_resource_count} resources) covering printer "
                    f"monitoring, file management, slicing, marketplaces, model "
                    f"generation, design intelligence, safety controls"
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
                    "total_mcp_capabilities": live_capability_count,
                    "prompts": live_prompt_count,
                    "resources": live_resource_count,
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
                "when_kiln_gets_it_wrong": {
                    "what": (
                        "Kiln has bugs, and the ones that matter most are "
                        "about hardware. Printer vendors publish very "
                        "little, so much of what Kiln knows about a "
                        "machine came from somebody hitting a wall and "
                        "working out what actually worked. When Kiln is "
                        "wrong about a printer, you are usually the first "
                        "person to find out — and you have the machine in "
                        "front of you."
                    ),
                    "do_both": (
                        "Work around it AND tell Kiln. Get the user their "
                        "print first — that is the job. Then send what "
                        "worked with report_issue. Withholding a "
                        "workaround to make a point is a worse product, "
                        "and keeping it only in your own memory means the "
                        "bug stays live for everyone else while you "
                        "personally never see it again."
                    ),
                    "how": (
                        "report_issue(description=..., context={'field_note': "
                        "{'observed': ..., 'tried': ..., 'worked': ..., "
                        "'evidence': ...}}). All four, or it is filed as an "
                        "ordinary report. observed = what Kiln did and any "
                        "error the machine reported; tried = what you "
                        "tested, changing ONE thing at a time; worked = "
                        "what fixed it; evidence = how you know."
                    ),
                    "be_honest_about_uncertainty": (
                        "A trial that changed two things at once cannot say "
                        "which one mattered — say so. An honest 'this "
                        "worked once and I changed two settings' is worth "
                        "more than a confident guess, because Kiln's team "
                        "can ask for the missing trial instead of shipping "
                        "a fix that was a coin flip."
                    ),
                    "privacy": (
                        "Send what Kiln's own tools got wrong. Never the "
                        "user's designs, files, or geometry — none of it "
                        "helps fix the bug."
                    ),
                    "no_account_needed": (
                        "report_issue works without signing in. An install "
                        "that cannot pair is exactly the one with something "
                        "to report."
                    ),
                },
                "core_workflows": {
                    "print_a_file": "upload_file → visualize_model → preflight_check → start_print",
                    "marketplace_to_print": (
                        "search_all_models → download_and_upload → preflight_check → start_print"
                    ),
                    "slice_and_print": "upload_file (STL) → slice_and_print",
                    "show_a_mesh_file_in_3d": (
                        "import_external_mesh(mesh_path) — brings an STL/OBJ/3MF/"
                        "STEP file in as a design AND opens the interactive 3D "
                        "stage on it (see inline_3d_stage below). Where that "
                        "tool isn't available (it ships with kiln-pro), "
                        "visualize_model gives the PNG plus a browser stage "
                        "link when signed in."
                    ),
                    "monitor": "printer_status, printer_snapshot, await_print_completion",
                    "queue_jobs": "submit_job → job_status → queue_summary",
                },
                "session_maintenance": {
                    "why": (
                        "Keeping the Kiln session itself healthy is agent work, "
                        "not user work. In particular: after Kiln's code, "
                        "config, or plugins change, restart the SERVER — do "
                        "not tell the user to quit and reopen their client "
                        "app. These lived only in the connect-time preamble "
                        "for a while, which is read once and lost; an agent "
                        "mid-session could not rediscover them, so the one "
                        "tool that applies updates went unused while users "
                        "were told to restart apps by hand."
                    ),
                    "tools": {
                        "restart_server": (
                            "restart_server() — hot-restart the MCP server "
                            "in-place; the client auto-reconnects in ~1s. Use "
                            "after code, plugin, or env/config changes."
                        ),
                        "kiln_health": (
                            "kiln_health() — versions, uptime, module "
                            "availability, safety-gate state."
                        ),
                        "trim_serve_processes": (
                            "trim_serve_processes(open_sessions=N) — close "
                            "leftover background server copies from closed "
                            "sessions; refuses while anything is printing."
                        ),
                        "upgrade_kiln": (
                            "upgrade_kiln() — update Kiln itself, then "
                            "restart_server() to run the new code."
                        ),
                        "kiln_signin": (
                            "kiln_signin() — browser sign-in for the hosted "
                            "features; kiln_signin_poll() completes it."
                        ),
                    },
                },
                "inline_3d_stage": {
                    "what": (
                        "Kiln has an interactive 3D stage — an inline panel the "
                        "user can orbit, zoom, and turn over — not just PNG "
                        "renders. Tools that make or change a mesh (the "
                        "generators, compile_scad, import_external_mesh, "
                        "decoration and repair tools) open it automatically on "
                        "success: inline in hosts that render MCP Apps panels "
                        "(Kiln's hosted connection also attaches a browser "
                        "stage link for hosts that don't). In a host without "
                        "panels on a local install, visualize_model attaches "
                        "a browser stage link of its own when the user is "
                        "signed in — so there is always some way to let them "
                        "turn the part over, and 'here is a PNG' is never the "
                        "end of the answer."
                    ),
                    "how_to_find_it": (
                        "The stage is attached to those tools' RESULTS (via "
                        "_meta and structured content), so it is not a separate "
                        "tool and won't show up under a name like 'viewer' — "
                        "never conclude from a tool search that Kiln can only "
                        "produce still images. Stage-opening tools say INLINE "
                        "3D STAGE in their descriptions."
                    ),
                    "show_me_this_file": (
                        "When the user asks to see a mesh file in 3D, the door "
                        "is import_external_mesh — importing IS showing. Heavy "
                        "meshes are decimated automatically for the stage; no "
                        "mesh is too big to try."
                    ),
                },
                "creating_models": {
                    "front_door": (
                        "When the user wants to MAKE an object, the flow is "
                        "always the same: understand what they want → make it → "
                        "SHOW a preview → iterate. If design_session is in your "
                        "available tools, use it as the front door (it captures "
                        "the goal, generates, checks, previews, and versions in "
                        "one place, defaulting to OpenSCAD-native). If it is NOT "
                        "in your tools (free local install), run the SAME loop "
                        "yourself with compile_scad — do not try to call "
                        "design_session. Either way, the template library "
                        "(see check_the_template_library_first), product "
                        "generators, image, and cloud are sub-paths under "
                        "this one flow, not separate starting points."
                    ),
                    "check_the_template_library_first": (
                        f"Kiln ships {_template_part_count()} ready-made "
                        "PARAMETRIC PARTS — brackets, bins, stands, cases, "
                        "hooks, jigs, clamps, gears, plumbing and garden "
                        "parts. They are easy to miss, so make checking "
                        "them a habit. When the "
                        "user wants a FUNCTIONAL PART to their own "
                        "dimensions ('a bracket for a 200 mm shelf', 'a bin "
                        "that fits my drawer', 'a clip for an 8 mm cable'), "
                        "call find_design_templates(use_case) BEFORE writing "
                        "any OpenSCAD, and offer a match unprompted rather "
                        "than waiting to be asked whether one exists. Then "
                        "generate_from_template(id, {...}) renders a "
                        "printable STL. It is FREE, runs locally through "
                        "OpenSCAD with no AI provider or API key, and every "
                        "part is print-verified at its default, minimum and "
                        "maximum parameters. No match is a normal answer — "
                        "fall back to writing OpenSCAD. (Results also "
                        "include design PATTERNS marked generatable:false — "
                        "snap fits, living hinges, press fits. Those are "
                        "guidance for OpenSCAD you write; "
                        "generate_from_template does not accept them.)"
                    ),
                    "default_is_openscad": (
                        "For a custom object the template library does not "
                        "cover, DEFAULT to "
                        "writing the OpenSCAD yourself and compiling it locally "
                        "with compile_scad — free, no API key, works for every "
                        "user. This is the reach for any 'make me a ...' "
                        "request no template fits. (compile_scad needs the "
                        "OpenSCAD binary; see "
                        "the 'openscad' field below if it isn't installed.)"
                    ),
                    "providers_are_opt_in": (
                        "Cloud AI text-to-3D / image-to-3D backends (Gemini, "
                        "Meshy, Tripo3D, Stability) are OPTIONAL and need the "
                        "USER'S OWN API key. Do NOT suggest them, and do NOT ask "
                        "the user to set a key, unless the user explicitly asks "
                        "or wants an organic / photo-based shape OpenSCAD can't "
                        "easily reach. If a generate_* tool returns NO_PROVIDER, "
                        "fall back to writing OpenSCAD — never tell the user to "
                        "go get a key."
                    ),
                    "from_an_image": (
                        "User gave you a reference photo or sketch? You can SEE "
                        "it — look at the image yourself and write the OpenSCAD "
                        "to match. You do NOT need an image-to-3D provider for "
                        "this. generate_model_from_image (Meshy) is an OPT-IN "
                        "for users who specifically want a photoreal mesh "
                        "reconstruction and bring their own key; it is never "
                        "required."
                    ),
                    "organic_shapes": (
                        "For curvy / organic / sculptural shapes (vases, "
                        "characters, anatomical forms), reach for the bundled "
                        "BOSL2 toolkit in your OpenSCAD — metaballs and skin() "
                        "build smooth organic geometry that plain primitives "
                        "can't. Organic shapes especially take a few refine "
                        "rounds — say so upfront and iterate with previews."
                    ),
                    "show_every_round": (
                        "Render and SHOW a preview image every time you make or "
                        "change a model — and after EVERY iteration round, "
                        "automatically, without being asked (visualize_model / "
                        "render_design_mesh). Mesh-producing tools also open "
                        "the interactive 3D stage on their own (see "
                        "inline_3d_stage) — the PNG is the floor, not the "
                        "ceiling. For complex or organic asks, tell "
                        "the user upfront that good results usually take a few "
                        "rounds — that's normal. Check each result yourself: "
                        "does it sit on the bed, is it printable, does it match "
                        "the ask."
                    ),
                    "iteration_loop": (
                        "After the first preview, offer the choice in plain "
                        "English: 'Want me to loop about 3 more times and then "
                        "check in, or keep looping until I think it's done?' Show "
                        "a fresh preview after each round so the user can stop "
                        "anytime, point at a version they liked, and say "
                        "'iterate from that one but change X.' Never grind "
                        "without showing previews; never stop at a bad result. "
                        "(Saving every version and branching from a past one is "
                        "a Pro feature — kiln3d.com/pricing; free users still get "
                        "the live preview loop and linear history.)"
                    ),
                    "three_kinds_to_save": (
                        "When the user wants to SAVE something reusable, there "
                        "are three kinds and they have different homes: a whole "
                        "printable object is a design; a reusable mechanical edit "
                        "or a reusable part that attaches to other objects (a "
                        "fillet, a clip, a hat that snaps onto a figure) is a "
                        "feature; a reusable visual layer that rides on a surface "
                        "(a logo, a photo, a pattern) is a decoration. Pick the "
                        "kind before you save; if it's ambiguous, ask the user one "
                        "short question first. Feature and decoration saving are "
                        "available where those tools are installed."
                    ),
                },
                "safety_tools": [
                    "preflight_check — validates printer readiness before printing",
                    "validate_gcode — checks G-code for dangerous commands before sending",
                    "safety_status — comprehensive safety dashboard (limits, rate-limits, blocked actions, auth)",
                    "safety_settings — shows current auto-print and confirmation settings",
                    "safety_audit — reviews recent safety-relevant actions",
                    (
                        "kiln_health — check `safety_profile.gates_active`. "
                        "If false, printer_model is unset in ~/.kiln/config.yaml "
                        "and Kiln can't check that prints fit the bed or stay "
                        "within safe temperatures.  Ask the user their printer "
                        "model (e.g. bambu_a1, prusa_mk4) and add "
                        "`printer_model: <value>` to the printer entry."
                    ),
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
