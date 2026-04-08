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

_logger = logging.getLogger(__name__)


class _EnterpriseToolsPlugin:
    """Enterprise administration and compliance tools.

    Tools:
        - export_audit_trail
        - lock_safety_profile
        - unlock_safety_profile
        - manage_team_member
        - printer_usage_summary
        - uptime_report
        - encryption_status
        - rotate_encryption_key
        - database_status
        - report_printer_overage
        - configure_sso
        - sso_login_url
        - sso_exchange_code
        - sso_status
    """

    @property
    def name(self) -> str:
        return "enterprise_tools"

    @property
    def description(self) -> str:
        return "Enterprise admin tools (audit, SSO, encryption, teams, billing)"

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

        @mcp.tool()
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
                action: Filter by action (executed, blocked, etc.).
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

        @mcp.tool()
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

        @mcp.tool()
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

        @mcp.tool()
        @requires_tier(LicenseTier.ENTERPRISE)
        def manage_team_member(
            action: str,
            email: str,
            role: str = "engineer",
        ) -> dict:
            """Add, remove, or update a team member.

            Enterprise feature. Manages team seats and role assignments.
            Business tier supports up to 5 seats; Enterprise is unlimited.

            Args:
                action: One of ``"add"``, ``"remove"``, ``"set_role"``, ``"list"``.
                email: Member email address (ignored for ``"list"``).
                role: Role for add/set_role: ``"admin"``, ``"engineer"``, ``"operator"``.
            """
            if err := _srv._check_auth("admin"):
                return err
            try:
                from kiln.licensing import get_tier
                from kiln.teams import TeamManager

                mgr = TeamManager()
                tier = get_tier().value

                if action == "list":
                    members = mgr.list_members()
                    seat_info = mgr.seat_status(tier=tier)
                    return {
                        "success": True,
                        "members": [m.to_dict() for m in members],
                        "seats": seat_info,
                    }
                elif action == "add":
                    member = mgr.add_member(email, role=role, tier=tier)
                    return {
                        "success": True,
                        "message": f"Added {email} as {role}.",
                        "member": member.to_dict(),
                    }
                elif action == "remove":
                    removed = mgr.remove_member(email)
                    if not removed:
                        return _srv._error_dict(f"No active member with email {email!r}.", code="NOT_FOUND")
                    return {
                        "success": True,
                        "message": f"Removed {email} from team.",
                    }
                elif action == "set_role":
                    member = mgr.set_member_role(email, role)
                    return {
                        "success": True,
                        "message": f"Updated {email} role to {role}.",
                        "member": member.to_dict(),
                    }
                else:
                    return _srv._error_dict(
                        f"Unknown action: {action!r}. Use add, remove, set_role, or list.",
                        code="INVALID_INPUT",
                    )
            except Exception as exc:
                _logger.exception("Error in manage_team_member")
                return _srv._error_dict(f"Team management failed: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # Printer usage & billing
        # ------------------------------------------------------------------

        @mcp.tool()
        @requires_tier(LicenseTier.ENTERPRISE)
        def printer_usage_summary() -> dict:
            """Show printer count, included allowance, and overage charges.

            Enterprise feature. Enterprise base includes 20 printers.
            Additional printers are $15/month each.
            """
            if err := _srv._check_auth("read"):
                return err
            try:
                from kiln.printer_billing import PrinterUsageBilling

                billing = PrinterUsageBilling()
                active_count = _srv._get_registry().count
                usage = billing.usage_summary(active_count)
                estimate = billing.estimate_monthly_cost(active_count)

                return {
                    "success": True,
                    "usage": usage.to_dict(),
                    "cost_estimate": estimate,
                }
            except Exception as exc:
                _logger.exception("Error in printer_usage_summary")
                return _srv._error_dict(f"Failed to get printer usage: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # Uptime
        # ------------------------------------------------------------------

        @mcp.tool()
        @requires_tier(LicenseTier.ENTERPRISE)
        def uptime_report() -> dict:
            """Get rolling uptime statistics and SLA status.

            Enterprise feature. Shows uptime percentages for 1h, 24h, 7d,
            and 30d windows, average response times, and whether the 99.9%
            SLA target is being met.
            """
            if err := _srv._check_auth("read"):
                return err
            try:
                from kiln.uptime import get_uptime_tracker

                tracker = get_uptime_tracker()
                report = tracker.uptime_report()
                incidents = tracker.recent_incidents(limit=5)

                return {
                    "success": True,
                    "uptime": report,
                    "recent_incidents": incidents,
                }
            except Exception as exc:
                _logger.exception("Error in uptime_report")
                return _srv._error_dict(f"Failed to get uptime report: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # G-code encryption
        # ------------------------------------------------------------------

        @mcp.tool()
        @requires_tier(LicenseTier.ENTERPRISE)
        def encryption_status() -> dict:
            """Check G-code encryption status and configuration.

            Enterprise feature. Reports whether encryption is active,
            whether the encryption key is configured, and whether the
            cryptography library is installed.
            """
            if err := _srv._check_auth("read"):
                return err
            try:
                from kiln.gcode_encryption import get_gcode_encryption

                enc = get_gcode_encryption()
                return {
                    "success": True,
                    "encryption": enc.status(),
                }
            except Exception as exc:
                _logger.exception("Error in encryption_status")
                return _srv._error_dict(f"Failed to get encryption status: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        @requires_tier(LicenseTier.ENTERPRISE)
        def rotate_encryption_key(
            old_passphrase: str,
            new_passphrase: str,
            directory: str,
            pattern: str = "*.gcode",
            dry_run: bool = True,
        ) -> dict:
            """Rotate the G-code encryption key by re-encrypting all files.

            Scans *directory* recursively for encrypted G-code files, decrypts
            with the old passphrase, and re-encrypts with the new one.

            **Run with ``dry_run=True`` first** to preview which files would be
            affected.  Then call again with ``dry_run=False`` to execute.

            After rotation, update the ``KILN_ENCRYPTION_KEY`` environment
            variable to the new passphrase and restart the server.

            Args:
                old_passphrase: The current KILN_ENCRYPTION_KEY value.
                new_passphrase: The new passphrase to encrypt with.
                directory: Root directory to scan for encrypted G-code files.
                pattern: Glob pattern for files to process (default ``"*.gcode"``).
                dry_run: Preview only — don't modify files (default ``True``).

            Requires Enterprise license and admin scope.
            """
            if err := _srv._check_auth("admin"):
                return err
            try:
                from kiln.gcode_encryption import GcodeEncryption

                enc = GcodeEncryption()
                result = enc.rotate_key(
                    old_passphrase=old_passphrase,
                    new_passphrase=new_passphrase,
                    directory=directory,
                    pattern=pattern,
                    dry_run=dry_run,
                )
                msg = f"{'Dry run: would rotate' if dry_run else 'Rotated'} {result['rotated']} file(s)."
                if result["failed"]:
                    msg += f" {result['failed']} file(s) failed."
                return {"success": result["failed"] == 0, "message": msg, **result}
            except Exception as exc:
                _logger.exception("Error in rotate_encryption_key")
                return _srv._error_dict(f"Key rotation failed: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # Database status
        # ------------------------------------------------------------------

        @mcp.tool()
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
        # Printer overage billing
        # ------------------------------------------------------------------

        @mcp.tool()
        @requires_tier(LicenseTier.ENTERPRISE)
        def report_printer_overage(
            subscription_item_id: str,
            active_printer_count: int | None = None,
        ) -> dict:
            """Report metered printer usage to Stripe for Enterprise billing.

            Enterprise feature. The first 20 printers are included in the base
            Enterprise price ($499/mo). This tool **automatically subtracts** the
            20 included printers and reports only the overage count to Stripe's
            ``active_printers`` meter at $15/printer/month.

            If *active_printer_count* is omitted, the fleet registry is queried
            automatically — no manual counting needed.

            Args:
                subscription_item_id: The Stripe SubscriptionItem ID (``si_...``) for
                    the metered printer overage line item on the customer's subscription.
                active_printer_count: Total number of active printers.  Leave empty
                    to auto-detect from the fleet registry.

            Example:
                With 25 registered printers, this reports **5** to Stripe
                (25 − 20 included = 5 overage × $15 = $75/mo).
            """
            if err := _srv._check_auth("admin"):
                return err
            try:
                from kiln.payments.stripe_provider import StripeProvider
                from kiln.printer_billing import INCLUDED_PRINTERS

                stripe_key = os.environ.get("KILN_STRIPE_SECRET_KEY", "")
                if not stripe_key:
                    return _srv._error_dict("Stripe not configured. Set KILN_STRIPE_SECRET_KEY.", code="CONFIG_MISSING")

                # Auto-detect fleet size if not provided.
                if active_printer_count is None:
                    active_printer_count = _srv._get_registry().count
                    if active_printer_count == 0:
                        return _srv._error_dict(
                            "No printers registered in the fleet. Register printers first or pass active_printer_count explicitly.",
                            code="NO_PRINTERS",
                        )

                provider = StripeProvider(secret_key=stripe_key)
                overage = max(0, active_printer_count - INCLUDED_PRINTERS)
                result = provider.report_printer_usage(subscription_item_id, overage)

                return {
                    "success": True,
                    "active_printers": active_printer_count,
                    "included": INCLUDED_PRINTERS,
                    "overage_reported_to_stripe": overage,
                    "overage_cost": f"${overage * 15:.2f}/mo",
                    "stripe_usage_record": result,
                    "note": f"Reported {overage} overage printers to Stripe (total {active_printer_count} minus {INCLUDED_PRINTERS} included).",
                }
            except Exception as exc:
                _logger.exception("Error in report_printer_overage")
                return _srv._error_dict(f"Failed to report usage: {exc}", code="PAYMENT_ERROR")

        # ------------------------------------------------------------------
        # SSO (Enterprise)
        # ------------------------------------------------------------------

        @mcp.tool()
        @requires_tier(LicenseTier.ENTERPRISE)
        def configure_sso(
            issuer_url: str,
            client_id: str,
            protocol: str = "oidc",
            client_secret: str = "",
            redirect_uri: str = "",
            allowed_domains: str = "",
            role_mapping: str = "",
        ) -> dict:
            """Configure SSO (OIDC or SAML) for Enterprise authentication.

            Enterprise feature. Sets up single sign-on with your identity provider
            (Okta, Google Workspace, Azure AD, Auth0, etc.).

            Args:
                issuer_url: IdP issuer URL (e.g. ``https://accounts.google.com``).
                client_id: OIDC client ID or SAML entity ID.
                protocol: ``"oidc"`` or ``"saml"``.
                client_secret: OIDC client secret (optional for public clients).
                redirect_uri: Callback URL after auth. Default: ``http://localhost:8741/sso/callback``.
                allowed_domains: Comma-separated email domains (e.g. ``"acme.com,partner.org"``).
                role_mapping: JSON string mapping IdP groups to Kiln roles
                    (e.g. ``'{"admins":"admin","devs":"engineer"}'``).
            """
            if err := _srv._check_auth("admin"):
                return err
            try:
                from kiln.sso import SSOConfig, SSOProtocol, get_sso_manager

                try:
                    proto = SSOProtocol(protocol.lower())
                except ValueError:
                    return _srv._error_dict(
                        f"Invalid protocol: {protocol!r}. Use 'oidc' or 'saml'.",
                        code="INVALID_INPUT",
                    )

                domains = [d.strip() for d in allowed_domains.split(",") if d.strip()] if allowed_domains else []
                mapping: dict[str, str] = {}
                if role_mapping:
                    import json as _json

                    try:
                        mapping = _json.loads(role_mapping)
                    except _json.JSONDecodeError:
                        return _srv._error_dict("role_mapping must be valid JSON.", code="INVALID_INPUT")

                config = SSOConfig(
                    protocol=proto,
                    issuer_url=issuer_url,
                    client_id=client_id,
                    client_secret=client_secret or None,
                    redirect_uri=redirect_uri or "http://localhost:8741/sso/callback",
                    allowed_domains=domains,
                    role_mapping=mapping,
                )

                mgr = get_sso_manager()
                mgr.configure(config)

                return {
                    "success": True,
                    "protocol": proto.value,
                    "issuer_url": issuer_url,
                    "allowed_domains": domains,
                    "next_step": (
                        "SSO configured. Use 'sso_login_url' to get the IdP login URL, "
                        "then exchange the auth code with 'sso_exchange_code'."
                    ),
                }
            except Exception as exc:
                _logger.exception("Error in configure_sso")
                return _srv._error_dict(f"Failed to configure SSO: {exc}", code="SSO_ERROR")

        @mcp.tool()
        @requires_tier(LicenseTier.ENTERPRISE)
        def sso_login_url(state: str = "") -> dict:
            """Get the SSO login URL to redirect users to the identity provider.

            Enterprise feature. Returns the IdP authorization URL for OIDC or
            the SAML AuthnRequest redirect URL.

            Args:
                state: Optional opaque state parameter for CSRF protection.
            """
            if err := _srv._check_auth("read"):
                return err
            try:
                from kiln.sso import SSOProtocol, get_sso_manager

                mgr = get_sso_manager()
                config = mgr.get_config()
                if config is None:
                    return _srv._error_dict("SSO not configured. Use 'configure_sso' first.", code="CONFIG_MISSING")

                if config.protocol == SSOProtocol.OIDC:
                    url = mgr.get_oidc_authorize_url(state=state or None)
                else:
                    url = mgr.get_saml_login_url()

                return {
                    "success": True,
                    "login_url": url,
                    "protocol": config.protocol.value,
                    "next_step": "Redirect the user to login_url. After auth, exchange the code with 'sso_exchange_code'.",
                }
            except Exception as exc:
                _logger.exception("Error in sso_login_url")
                return _srv._error_dict(f"Failed to generate login URL: {exc}", code="SSO_ERROR")

        @mcp.tool()
        @requires_tier(LicenseTier.ENTERPRISE)
        def sso_exchange_code(code: str) -> dict:
            """Exchange an SSO authorization code for user identity and role.

            Enterprise feature. After the user completes IdP login, exchange
            the auth code to get their identity, email, groups, and mapped
            Kiln role.

            Args:
                code: The authorization code from the IdP callback.
            """
            if err := _srv._check_auth("read"):
                return err
            try:
                from kiln.sso import get_sso_manager, map_sso_user_to_role

                mgr = get_sso_manager()
                config = mgr.get_config()
                if config is None:
                    return _srv._error_dict("SSO not configured. Use 'configure_sso' first.", code="CONFIG_MISSING")

                user = mgr.exchange_oidc_code(code)
                kiln_role = map_sso_user_to_role(user)

                return {
                    "success": True,
                    "user": user.to_dict(),
                    "kiln_role": kiln_role,
                    "next_step": f"User authenticated as {user.email} with role '{kiln_role}'.",
                }
            except Exception as exc:
                _logger.exception("Error in sso_exchange_code")
                return _srv._error_dict(f"SSO authentication failed: {exc}", code="SSO_ERROR")

        @mcp.tool()
        @requires_tier(LicenseTier.ENTERPRISE)
        def sso_status() -> dict:
            """Check current SSO configuration status.

            Enterprise feature. Returns whether SSO is configured, the protocol,
            issuer, allowed domains, and role mapping.
            """
            if err := _srv._check_auth("read"):
                return err
            try:
                from kiln.sso import get_sso_manager

                mgr = get_sso_manager()
                config = mgr.get_config()
                if config is None:
                    return {
                        "success": True,
                        "configured": False,
                        "next_step": "SSO not configured. Use 'configure_sso' to set up OIDC or SAML.",
                    }

                return {
                    "success": True,
                    "configured": True,
                    "protocol": config.protocol.value,
                    "issuer_url": config.issuer_url,
                    "client_id": config.client_id,
                    "allowed_domains": config.allowed_domains,
                    "role_mapping": config.role_mapping,
                    "redirect_uri": config.redirect_uri,
                }
            except Exception as exc:
                _logger.exception("Error in sso_status")
                return _srv._error_dict(f"Failed to get SSO status: {exc}", code="SSO_ERROR")

        _logger.debug("Registered enterprise tools")


plugin = _EnterpriseToolsPlugin()
