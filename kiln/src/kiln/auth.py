"""API key authentication for the Kiln MCP server.

Provides a simple, optional authentication layer. When enabled, clients
must provide a valid API key to use MCP tools. Keys are stored locally
in the SQLite database.

Authentication is disabled by default. Set KILN_AUTH_ENABLED=1 and
KILN_AUTH_KEY=<your-key> to enable.

Keys can also be managed programmatically:

    auth = AuthManager()
    auth.create_key("my-agent", scopes=["read", "write"])
    auth.verify("sk_abc123...")
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Prefix for generated keys
_KEY_PREFIX = "sk_kiln_"


@dataclass
class ApiKey:
    """A single API key."""
    id: str
    name: str
    key_hash: str  # SHA-256 hash of the actual key
    scopes: Set[str]  # e.g. {"read", "write", "admin"}
    active: bool = True
    created_at: float = field(default_factory=time.time)
    last_used_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["scopes"] = sorted(self.scopes)
        return data


class AuthError(Exception):
    """Raised when authentication fails."""
    pass


class AuthManager:
    """Manages API keys and authentication.

    Supports two modes:
    1. Simple mode: Single key via KILN_AUTH_KEY env var
    2. Multi-key mode: Multiple keys stored in memory (extensible to SQLite)
    """

    def __init__(self, enabled: Optional[bool] = None) -> None:
        self._enabled = enabled if enabled is not None else (
            os.environ.get("KILN_AUTH_ENABLED", "").lower() in ("1", "true", "yes")
        )
        self._keys: Dict[str, ApiKey] = {}  # key_hash -> ApiKey
        self._env_key_hash: Optional[str] = None

        # Load env key if set
        env_key = os.environ.get("KILN_AUTH_KEY", "")
        if env_key:
            self._env_key_hash = self._hash_key(env_key)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    @staticmethod
    def _hash_key(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    @staticmethod
    def generate_key() -> str:
        """Generate a new random API key."""
        return _KEY_PREFIX + secrets.token_hex(24)

    def create_key(
        self,
        name: str,
        scopes: Optional[List[str]] = None,
    ) -> tuple[ApiKey, str]:
        """Create a new API key.

        Returns (ApiKey metadata, raw key string).
        The raw key is only shown once -- store it securely.
        """
        raw_key = self.generate_key()
        key_hash = self._hash_key(raw_key)
        key_id = secrets.token_hex(6)

        api_key = ApiKey(
            id=key_id,
            name=name,
            key_hash=key_hash,
            scopes=set(scopes or ["read", "write"]),
        )
        self._keys[key_hash] = api_key
        logger.info("Created API key %r (id=%s)", name, key_id)
        return api_key, raw_key

    def revoke_key(self, key_id: str) -> bool:
        """Revoke (deactivate) a key by ID."""
        for api_key in self._keys.values():
            if api_key.id == key_id:
                api_key.active = False
                logger.info("Revoked API key %r (id=%s)", api_key.name, key_id)
                return True
        return False

    def delete_key(self, key_id: str) -> bool:
        """Permanently delete a key by ID."""
        to_remove = None
        for key_hash, api_key in self._keys.items():
            if api_key.id == key_id:
                to_remove = key_hash
                break
        if to_remove:
            del self._keys[to_remove]
            return True
        return False

    def list_keys(self) -> List[ApiKey]:
        """Return all registered keys (without the raw key values)."""
        return list(self._keys.values())

    def verify(self, key: str, required_scope: Optional[str] = None) -> ApiKey:
        """Verify an API key and optionally check scope.

        Returns the ApiKey if valid.
        Raises AuthError if invalid, revoked, or missing scope.
        """
        if not self._enabled:
            # Auth disabled -- return a permissive stub
            return ApiKey(id="none", name="auth-disabled", key_hash="", scopes={"admin"})

        if not key:
            raise AuthError("API key required")

        key_hash = self._hash_key(key)

        # Check env key first
        if self._env_key_hash and hmac.compare_digest(key_hash, self._env_key_hash):
            return ApiKey(
                id="env",
                name="environment-key",
                key_hash=self._env_key_hash,
                scopes={"read", "write", "admin"},
            )

        # Check registered keys
        api_key = self._keys.get(key_hash)
        if api_key is None:
            raise AuthError("Invalid API key")

        if not api_key.active:
            raise AuthError("API key has been revoked")

        if required_scope and required_scope not in api_key.scopes:
            raise AuthError(f"Key missing required scope: {required_scope!r}")

        api_key.last_used_at = time.time()
        return api_key

    def check_request(self, key: Optional[str] = None, scope: Optional[str] = None) -> Dict[str, Any]:
        """Check auth for a request. Returns a dict suitable for MCP responses.

        If auth is disabled, returns {"authenticated": True, ...}.
        If key is valid, returns {"authenticated": True, "key_name": ...}.
        If key is invalid, returns {"authenticated": False, "error": ...}.
        """
        if not self._enabled:
            return {"authenticated": True, "auth_enabled": False}

        if not key:
            return {
                "authenticated": False,
                "auth_enabled": True,
                "error": "API key required. Set via KILN_AUTH_KEY or create one with the auth manager.",
            }

        try:
            api_key = self.verify(key, required_scope=scope)
            return {
                "authenticated": True,
                "auth_enabled": True,
                "key_name": api_key.name,
                "key_id": api_key.id,
                "scopes": sorted(api_key.scopes),
            }
        except AuthError as exc:
            return {
                "authenticated": False,
                "auth_enabled": True,
                "error": str(exc),
            }
