"""Credential storage tools plugin.

Extracts credential-management MCP tools from server.py into a focused
plugin module.  Provides encrypted storage and retrieval of API keys,
webhook secrets, and other sensitive values.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _CredentialToolsPlugin:
    """Credential storage and retrieval tools.

    Tools:
        - store_credential
        - list_credentials
        - retrieve_credential
    """

    @property
    def name(self) -> str:
        return "credential_tools"

    @property
    def description(self) -> str:
        return "Credential storage and retrieval tools"

    def register(self, mcp: Any) -> None:
        """Register credential tools with the MCP server."""

        import kiln.server as _srv

        # ------------------------------------------------------------------
        # store_credential
        # ------------------------------------------------------------------

        @mcp.tool()
        def store_credential(
            credential_type: str,
            value: str,
            *,
            label: str = "",
        ) -> dict:
            """Encrypt and store a credential (API key, webhook secret, etc.).

            The value is encrypted at rest using PBKDF2 + XOR stream encryption.
            Only metadata is returned — the plaintext is never exposed.

            Args:
                credential_type: Type of credential (api_key, webhook_secret,
                    stripe_key, marketplace_token, printer_password).
                value: The plaintext secret to store.
                label: Human-readable description.
            """
            if err := _srv._check_auth("admin"):
                return err

            try:
                from kiln.credential_store import CredentialType
                from kiln.credential_store import store_credential as _store

                try:
                    ctype = CredentialType(credential_type)
                except ValueError:
                    return _srv._error_dict(
                        f"Invalid type: {credential_type!r}. Valid: {[t.value for t in CredentialType]}",
                        code="VALIDATION_ERROR",
                    )
                cred = _store(ctype, value, label=label)
                return {"success": True, "credential": cred.to_dict()}
            except Exception as exc:
                _logger.exception("Error in store_credential")
                return _srv._error_dict(f"Failed to store credential: {exc}", code="CREDENTIAL_ERROR")

        # ------------------------------------------------------------------
        # list_credentials
        # ------------------------------------------------------------------

        @mcp.tool()
        def list_credentials() -> dict:
            """List all stored credentials (metadata only, no plaintext)."""
            try:
                from kiln.credential_store import get_credential_store

                store = get_credential_store()
                creds = store.list_credentials()
                return {
                    "success": True,
                    "credentials": [c.to_dict() for c in creds],
                    "count": len(creds),
                }
            except Exception as exc:
                _logger.exception("Error in list_credentials")
                return _srv._error_dict(f"Failed to list credentials: {exc}", code="CREDENTIAL_ERROR")

        # ------------------------------------------------------------------
        # retrieve_credential
        # ------------------------------------------------------------------

        @mcp.tool()
        def retrieve_credential(credential_id: str) -> dict:
            """Decrypt and return a stored credential.

            Args:
                credential_id: The credential's unique identifier.
            """
            import kiln.server as _srv
            if err := _srv._check_auth("admin"):
                return err

            try:
                from kiln.credential_store import retrieve_credential as _retrieve

                value = _retrieve(credential_id)
                return {"success": True, "credential_id": credential_id, "value": value}
            except Exception as exc:
                _logger.exception("Error in retrieve_credential")
                return _srv._error_dict(f"Failed to retrieve credential: {exc}", code="CREDENTIAL_ERROR")


plugin = _CredentialToolsPlugin()
