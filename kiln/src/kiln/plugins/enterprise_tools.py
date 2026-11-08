"""Enterprise admin tools plugin.

Extracts enterprise-tier MCP tools from server.py into a focused plugin
module.  Provides tools for audit trail export, safety profile locking,
team management, printer billing, uptime monitoring, G-code encryption,
database status, and SSO configuration.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from kiln.tool_annotations import destructive, read_only

_logger = logging.getLogger(__name__)


_ENCRYPTION_KEY_ENV = "KILN_ENCRYPTION_KEY"


#: What is true of the files a person keeps in their Kiln account, on every
#: install and at every tier: the server that stores them encrypts them, with
#: a key the server holds.  This tool otherwise answers only about THIS
#: computer, so the sentence is carried separately and never changes with
#: the local key.
_ACCOUNT_FILES_SENTENCE = (
    "Print files you keep in your Kiln account are encrypted on Kiln's "
    "servers with a key held there."
)


def _encryption_not_installed() -> dict[str, Any]:
    """The answer to ``encryption_status`` where encryption at rest is not
    installed: the same keys the full answer carries, every one of them
    true of this machine, and a sentence saying what that means.

    Encryption is applied by the machine that stores the G-code, so there
    is nothing to ask anywhere else: a file written here is not encrypted.
    The files kept in the account are another machine's -- Kiln's servers
    encrypt those -- and the answer says so beside the local verdict.
    """
    import importlib.util

    key_set = bool(os.environ.get(_ENCRYPTION_KEY_ENV, "").strip())
    message = "G-code encryption at rest is not installed on this machine, so G-code files here are stored unencrypted."
    if key_set:
        message += f" {_ENCRYPTION_KEY_ENV} is set, but nothing on this machine uses it."
    message += f" {_ACCOUNT_FILES_SENTENCE}"
    message += " Encryption at rest is part of Kiln Enterprise: https://kiln3d.com/pricing"
    return {
        "success": True,
        "encryption": {
            "available": False,
            "key_configured": key_set,
            "library_installed": importlib.util.find_spec("cryptography") is not None,
            "supports_rotation": False,
        },
        "account_files": _ACCOUNT_FILES_SENTENCE,
        "message": message,
    }


#: The extra-printer figure is worked out by Kiln's servers, for the plan the
#: account holds (``preview_overage_charge``, free on every plan).  When it
#: does not come back, what the person reads: what is at stake, what Kiln
#: could not do, and what it therefore does not show.
_FIGURE_WORDS = {
    "on_the_line": "What extra printers add to your bill on your plan is worked out by Kiln's servers",
    "cannot": "work out your figure",
    "wont": "shows no figure for your plan",
}
_FIGURE_TOOL = "preview_overage_charge"


def _usage_from_the_servers(printers: int, plan: str) -> dict[str, Any]:
    """``printer_usage_summary`` on an install that cannot price printers
    itself: the servers' figure for *printers* on the account's own plan,
    or the count with why there is no figure, said once, and where the
    prices are.  A figure that did not come back is never a number."""
    from kiln import served_public
    from kiln.served_answer import classify_answer, fields, sentence
    from kiln.tiers_and_terms import PRICING_URL, signin_hint_fields

    answer, miss = served_public.answer_or_missed(_FIGURE_TOOL, {"active_printer_count": printers})
    if answer is not None:
        miss = classify_answer(answer)
        if miss is None:
            return {**answer, "success": True}
    noun = "printer is" if printers == 1 else "printers are"
    why = sentence(miss, feature="servers", then="ask again", **_FIGURE_WORDS)
    out: dict[str, Any] = {
        "success": True,
        "printers": printers,
        "your_plan": plan,
        "summary": (
            f"{printers} {noun} registered with this Kiln. {why} "
            f"What extra printers cost on each plan: {PRICING_URL}"
        ),
        "pricing_url": PRICING_URL,
        **fields(miss),
    }
    if miss.cause == "signed_out":
        out.update(signin_hint_fields())
    return out


class _EnterpriseToolsPlugin:
    """Enterprise administration and compliance tools.

    Tools:
        - export_audit_trail
        - lock_safety_profile
        - unlock_safety_profile
        - printer_usage_summary
        - encryption_status
        - database_status

    Retired (``kiln.retired_tools.RETIRED`` says where each job
    lives now): manage_team_member, uptime_report, rotate_encryption_key,
    configure_sso, sso_login_url, sso_exchange_code, sso_status.  Each
    reached an engine that ships only with kiln-pro, so on every customer's
    install it answered "No module named"; the jobs live on the account's
    web pages and the served org tools.
    """

    @property
    def name(self) -> str:
        return "enterprise_tools"

    @property
    def description(self) -> str:
        return "Enterprise admin tools (audit, safety-profile locks, encryption status, billing)"

    def register(self, mcp: Any) -> None:  # noqa: PLR0915
        """Register enterprise tools with the MCP server."""

        # Lazy-import tier gating at registration time so the decorator is
        # available for all tool definitions below.
        import kiln.server as _srv

        requires_tier = _srv.requires_tier
        LicenseTier = _srv.LicenseTier

        # ------------------------------------------------------------------
        # Audit trail
        # ------------------------------------------------------------------

        @mcp.tool(annotations=read_only("Export safety audit trail"))
        @requires_tier(LicenseTier.ENTERPRISE)
        def export_audit_trail(
            start_time: float = 0,
            end_time: float = 0,
            format: str = "json",
            tool_name: str = "",
            action: str = "",
            session_id: str = "",
        ) -> dict:
            """Export the safety audit trail as JSON or CSV.

            Enterprise feature. Returns the full audit log with optional filters
            for date range, tool name, action type, and session ID.

            Args:
                start_time: Unix timestamp lower bound (0 = no filter).
                end_time: Unix timestamp upper bound (0 = no filter).
                format: Output format, ``"json"`` or ``"csv"``.
                tool_name: Filter by MCP tool name.
                action: Filter by action: ``"executed"``, ``"blocked"``,
                    ``"rate_limited"``, ``"auth_denied"``,
                    ``"preflight_failed"`` or ``"dry_run"``.  Empty for all.
                session_id: Filter by agent session ID.
            """
            if err := _srv._check_auth("admin"):
                return err
            try:
                db = _srv.get_db()
                exported = db.export_audit_trail(
                    start_time=start_time if start_time > 0 else None,
                    end_time=end_time if end_time > 0 else None,
                    format=format,
                    tool_name=tool_name or None,
                    action=action or None,
                    session_id=session_id or None,
                )
                return {
                    "success": True,
                    "format": format,
                    "data": exported,
                }
            except Exception as exc:
                _logger.exception("Error in export_audit_trail")
                return _srv._error_dict(f"Failed to export audit trail: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # Safety profile locking
        # ------------------------------------------------------------------

        @mcp.tool(annotations=destructive("Lock a safety profile"))
        @requires_tier(LicenseTier.ENTERPRISE)
        def lock_safety_profile(printer_model: str) -> dict:
            """Lock a safety profile so agents cannot modify its limits.

            Enterprise feature. When locked, community profile updates for this
            printer model are rejected. Only an admin can unlock.

            Args:
                printer_model: Profile identifier to lock (e.g. "ender3").
            """
            if err := _srv._check_auth("admin"):
                return err
            try:
                from kiln.safety_profiles import lock_safety_profile as _lock

                _lock(printer_model)
                return {
                    "success": True,
                    "message": f"Safety profile '{printer_model}' is now locked.",
                    "printer_model": printer_model,
                }
            except Exception as exc:
                _logger.exception("Error in lock_safety_profile")
                return _srv._error_dict(f"Failed to lock safety profile: {exc}", code="INTERNAL_ERROR")

        @mcp.tool(annotations=destructive("Unlock a safety profile"))
        @requires_tier(LicenseTier.ENTERPRISE)
        def unlock_safety_profile(printer_model: str) -> dict:
            """Unlock a previously locked safety profile.

            Enterprise feature. Allows community profile modifications for
            this printer model again.

            Args:
                printer_model: Profile identifier to unlock.
            """
            if err := _srv._check_auth("admin"):
                return err
            try:
                from kiln.safety_profiles import unlock_safety_profile as _unlock

                unlocked = _unlock(printer_model)
                if not unlocked:
                    return {
                        "success": True,
                        "message": f"Profile '{printer_model}' was not locked.",
                        "printer_model": printer_model,
                    }
                return {
                    "success": True,
                    "message": f"Safety profile '{printer_model}' is now unlocked.",
                    "printer_model": printer_model,
                }
            except Exception as exc:
                _logger.exception("Error in unlock_safety_profile")
                return _srv._error_dict(f"Failed to unlock safety profile: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # Team management
        # ------------------------------------------------------------------

        @mcp.tool(annotations=read_only("Printer usage summary"))
        def printer_usage_summary() -> dict:
            """How many printers are registered with this Kiln, and what
            they add to your bill on your plan, at your plan's prices.  Free
            on every plan.

            Counts the machines registered here, so ask it on the computer
            your printers are attached to.  When the figure cannot be had
            (offline, signed out), the count comes back with why, and where
            the prices are.
            """
            if err := _srv._check_auth("read"):
                return err
            from kiln.licensing import get_tier

            try:
                from kiln_pro.billing.printer_billing import extra_printer_cost
            except ImportError:
                extra_printer_cost = None
            try:
                printers = _srv._get_registry().count
                plan = str(getattr(get_tier(), "value", "free"))
                if extra_printer_cost is not None:
                    return {"success": True, **extra_printer_cost(plan, printers)}
                return _usage_from_the_servers(printers, plan)
            except Exception as exc:
                _logger.exception("Error in printer_usage_summary")
                return _srv._error_dict(f"Failed to get printer usage: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # Uptime
        # ------------------------------------------------------------------

        @mcp.tool(annotations=read_only("Encryption status"))
        @requires_tier(LicenseTier.ENTERPRISE)
        def encryption_status() -> dict:
            """Check G-code encryption status and configuration.

            Enterprise feature. Reports whether encryption is active on
            this computer, whether the local encryption key is configured,
            and whether the cryptography library is installed.  On a
            machine where encryption at rest is not installed, it answers
            that encryption is not active here, in the same shape, and
            says what enables it.  ``account_files`` says, on every
            install, what is true of the print files kept in the person's
            Kiln account: Kiln's servers encrypt those with a key held
            there.  Local files on this computer are encrypted only when
            the local key is set and encryption is installed here.
            """
            if err := _srv._check_auth("read"):
                return err
            try:
                try:
                    from kiln.gcode_encryption import get_gcode_encryption
                except ImportError:
                    return _encryption_not_installed()

                enc = get_gcode_encryption()
                return {
                    "success": True,
                    "encryption": enc.status(),
                    "account_files": _ACCOUNT_FILES_SENTENCE,
                }
            except Exception as exc:
                _logger.exception("Error in encryption_status")
                return _srv._error_dict(f"Failed to get encryption status: {exc}", code="INTERNAL_ERROR")

        @mcp.tool(annotations=read_only("Database status"))
        @requires_tier(LicenseTier.ENTERPRISE)
        def database_status() -> dict:
            """Check database backend status and configuration.

            Reports whether Kiln is using SQLite or PostgreSQL, the connection
            status, and key metrics.  Useful for verifying a PostgreSQL migration
            or diagnosing connectivity issues.

            Requires Enterprise license.
            """
            try:
                db = _srv.get_db()
                backend = "postgresql" if db._is_postgres else "sqlite"
                info: dict[str, Any] = {
                    "success": True,
                    "backend": backend,
                }
                if backend == "sqlite":
                    info["db_path"] = db._db_path
                    info["note"] = (
                        "SQLite is single-writer. Set KILN_POSTGRES_DSN for multi-replica HA."
                    )
                else:
                    info["note"] = "PostgreSQL backend active. Multi-replica scaling supported."

                # Quick health check — count audit entries as a connectivity test.
                try:
                    row = db._conn.execute("SELECT COUNT(*) FROM safety_audit_log").fetchone()
                    info["audit_entries"] = row[0] if row else 0
                    info["connected"] = True
                except Exception:
                    info["connected"] = False

                return info
            except Exception as exc:
                _logger.exception("Error in database_status")
                return _srv._error_dict(f"Failed to get database status: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # SSO (Enterprise)
        # ------------------------------------------------------------------

        _logger.debug("Registered enterprise tools")


plugin = _EnterpriseToolsPlugin()
