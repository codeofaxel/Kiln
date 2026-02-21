"""API key authentication for the Kiln MCP server.

Provides a simple, optional authentication layer. When enabled, clients
must provide a valid API key to use MCP tools. Keys are stored in memory
for the lifetime of the process (session-only — keys do not persist
across restarts).

Authentication is disabled by default. Set KILN_AUTH_ENABLED=1 and
KILN_AUTH_KEY=<your-key> to enable. When enabled without an explicit
key, a random session key is auto-generated for the process.

Keys can also be managed programmatically:

    auth = AuthManager()
    auth.create_key("my-agent", scopes=["read", "write"])
    auth.verify("sk_abc123...")

Key rotation is supported with a configurable grace period:

    auth.rotate_key("sk_old_key...", "sk_new_key...", grace_period=86400)
    auth.list_keys()     # shows status: active / deprecated / expired
    auth.revoke_key("sk_old_key...")  # immediately invalidate

.. note::

   Created/rotated/revoked keys are session-only and will be lost on
   restart. The ``KILN_AUTH_KEY`` env var key is always available across
   restarts. SQLite persistence for managed keys is planned for a future
   release.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Prefix for generated keys
_KEY_PREFIX = "sk_kiln_"

# Default grace period for key rotation (24 hours)
_DEFAULT_GRACE_PERIOD = 86400  # seconds

# Scope normalization so legacy feature scopes remain compatible with the
# canonical role scopes: read/write/admin.
_SCOPE_ALIAS_TO_CANONICAL: dict[str, str] = {
    "read": "read",
    "write": "write",
    "admin": "admin",
    # Read-only intelligence queries
    "intel": "read",
    # Feature scopes that imply mutating capability
    "files": "write",
    "print": "write",
    "temperature": "write",
    "billing": "write",
    "generate": "write",
    "firmware": "write",
    "history": "write",
    "monitoring": "write",
    "safety": "write",
    "gcode": "write",
    "slicer": "write",
    "pipeline": "write",
    "cache": "write",
    "config": "write",
    "calibrate": "write",
}


def _normalize_scope(scope: str | None) -> str | None:
    """Normalize a scope string to a canonical representation."""
    if scope is None:
        return None
    cleaned = scope.strip().lower()
    if not cleaned:
        return None
    return _SCOPE_ALIAS_TO_CANONICAL.get(cleaned, cleaned)


def _normalize_scope_set(scopes: set[str]) -> set[str]:
    """Return a cleaned, lowercase scope set with empty values removed."""
    normalized: set[str] = set()
    for scope in scopes:
        cleaned = (scope or "").strip().lower()
        if cleaned:
            normalized.add(cleaned)
    return normalized


def _expand_effective_scopes(scopes: set[str]) -> set[str]:
    """Expand scopes with aliases and hierarchical implications."""
    effective = set(scopes)
    canonical = {_normalize_scope(s) for s in scopes}
    effective.update(s for s in canonical if s)

    # Hierarchy: admin => write => read
    if "admin" in effective:
        effective.update({"write", "read"})
    if "write" in effective:
        effective.add("read")
    return effective


def _scope_satisfied(required_scope: str | None, granted_scopes: set[str]) -> bool:
    """Whether *granted_scopes* satisfy *required_scope*."""
    if required_scope is None:
        return True
    normalized_required = _normalize_scope(required_scope)
    effective = _expand_effective_scopes(_normalize_scope_set(granted_scopes))
    return required_scope in effective or (normalized_required in effective if normalized_required else False)


# ---------------------------------------------------------------------------
# Role-based access control (Enterprise)
# ---------------------------------------------------------------------------

class Role(str, Enum):
    """Named roles for team members.

    Each role maps to a fixed set of scopes. Enterprise tier only.
    """

    ADMIN = "admin"
    ENGINEER = "engineer"
    OPERATOR = "operator"


#: Scopes granted to each role.  Admin is a superset of engineer,
#: which is a superset of operator.
ROLE_SCOPES: dict[Role, set[str]] = {
    Role.ADMIN: {"read", "write", "admin"},
    Role.ENGINEER: {"read", "write"},
    Role.OPERATOR: {"read"},
}


@dataclass
class ApiKey:
    """A single API key."""

    id: str
    name: str
    key_hash: str  # SHA-256 hash of the actual key
    scopes: set[str]  # e.g. {"read", "write", "admin"}
    active: bool = True
    created_at: float = field(default_factory=time.time)
    last_used_at: float | None = None
    deprecated_at: float | None = None
    expires_at: float | None = None
    role: str | None = None  # Role name when RBAC is active

    @property
    def status(self) -> Literal["active", "deprecated", "expired", "revoked"]:
        """Derive current key status from metadata."""
        if not self.active:
            return "revoked"
        if self.expires_at is not None and time.time() >= self.expires_at:
            return "expired"
        if self.deprecated_at is not None:
            return "deprecated"
        return "active"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["scopes"] = sorted(self.scopes)
        data["status"] = self.status
        return data


class AuthError(Exception):
    """Raised when authentication fails."""

    pass


class AuthManager:
    """Manages API keys and authentication.

    Supports two modes:
    1. Simple mode: Single key via KILN_AUTH_KEY env var (persists via env)
    2. Multi-key mode: Multiple keys stored in memory (session-only)
    """

    def __init__(self, enabled: bool | None = None) -> None:
        self._enabled = (
            enabled
            if enabled is not None
            else (os.environ.get("KILN_AUTH_ENABLED", "").lower() in ("1", "true", "yes"))
        )
        self._keys: dict[str, ApiKey] = {}  # key_hash -> ApiKey
        self._env_key_hash: str | None = None
        self._generated_key: str | None = None

        # Load env key if set, otherwise auto-generate when auth is enabled
        env_key = os.environ.get("KILN_AUTH_KEY", "")
        if env_key:
            self._env_key_hash = self._hash_key(env_key)
        elif self._enabled:
            # Auto-generate a session key so auth works out of the box
            self._generated_key = secrets.token_urlsafe(32)
            self._env_key_hash = self._hash_key(self._generated_key)
            logger.warning(
                "Auth enabled but no KILN_AUTH_KEY set. "
                "Auto-generated ephemeral session key for this process. "
                "Set KILN_AUTH_KEY explicitly for stable, recoverable auth.",
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def generated_key(self) -> str | None:
        """Return the auto-generated session key, if one was created."""
        return self._generated_key

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
        scopes: list[str] | None = None,
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
            scopes=_normalize_scope_set(set(scopes or ["read", "write"])),
        )
        self._keys[key_hash] = api_key
        logger.info("Created API key %r (id=%s)", name, key_id)
        return api_key, raw_key

    def rotate_key(
        self,
        old_key: str,
        new_key: str | None = None,
        grace_period: float = _DEFAULT_GRACE_PERIOD,
    ) -> tuple[ApiKey, str]:
        """Rotate an API key: deprecate the old one and activate a new one.

        During the grace period, both old and new keys are accepted.
        After the grace period, the old key stops working.

        Args:
            old_key: The raw old API key string.
            new_key: Optional raw new key. If ``None``, a new key is generated.
            grace_period: Seconds the old key remains valid (default 24 hours).

        Returns:
            (new ApiKey metadata, raw new key string).

        Raises:
            AuthError: If the old key is not found, already revoked, or expired.
        """
        old_hash = self._hash_key(old_key)
        old_api_key = self._keys.get(old_hash)

        if old_api_key is None:
            raise AuthError("Old key not found — cannot rotate an unregistered key")

        old_status = old_api_key.status
        if old_status == "revoked":
            raise AuthError("Old key has been revoked — cannot rotate")
        if old_status == "expired":
            raise AuthError("Old key has already expired — cannot rotate")

        # Mark old key as deprecated with expiry
        now = time.time()
        old_api_key.deprecated_at = now
        old_api_key.expires_at = now + grace_period
        logger.info(
            "Deprecated API key %r (id=%s), expires in %.0fs",
            old_api_key.name,
            old_api_key.id,
            grace_period,
        )

        # Create the replacement key
        raw_new = new_key if new_key else self.generate_key()
        new_hash = self._hash_key(raw_new)
        new_key_id = secrets.token_hex(6)

        new_api_key = ApiKey(
            id=new_key_id,
            name=f"{old_api_key.name} (rotated)",
            key_hash=new_hash,
            scopes=set(old_api_key.scopes),
        )
        self._keys[new_hash] = new_api_key
        logger.info("Created rotated API key (id=%s) replacing (id=%s)", new_key_id, old_api_key.id)
        return new_api_key, raw_new

    def revoke_key(self, key_id: str) -> bool:
        """Revoke (deactivate) a key by its internal ID.

        Returns True if the key was found and revoked, False otherwise.
        """
        for api_key in self._keys.values():
            if api_key.id == key_id:
                api_key.active = False
                api_key.deprecated_at = api_key.deprecated_at or time.time()
                logger.info("Revoked API key %r (id=%s)", api_key.name, key_id)
                return True
        return False

    def revoke_key_by_raw(self, raw_key: str) -> bool:
        """Immediately invalidate a key by its raw key string.

        Returns True if the key was found and revoked, False otherwise.
        """
        key_hash = self._hash_key(raw_key)
        api_key = self._keys.get(key_hash)
        if api_key is None:
            return False
        api_key.active = False
        api_key.deprecated_at = api_key.deprecated_at or time.time()
        logger.info("Revoked API key %r (id=%s)", api_key.name, api_key.id)
        return True

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

    def list_keys(self) -> list[ApiKey]:
        """Return all registered keys.

        Use ``ApiKey.to_dict()`` on individual entries if you need a
        serialisable representation.
        """
        return list(self._keys.values())

    def verify(self, key: str, required_scope: str | None = None) -> ApiKey:
        """Verify an API key and optionally check scope.

        Returns the ApiKey if valid.
        Raises AuthError if invalid, revoked, expired, or missing scope.
        """
        if not self._enabled:
            # Auth disabled -- return a permissive stub
            return ApiKey(id="none", name="auth-disabled", key_hash="", scopes={"read", "write"})

        if not key:
            raise AuthError("API key required")

        key_hash = self._hash_key(key)

        # Check env key first
        if self._env_key_hash and hmac.compare_digest(key_hash, self._env_key_hash):
            env_api_key = ApiKey(
                id="env",
                name="environment-key",
                key_hash=self._env_key_hash,
                scopes={"read", "write", "admin"},
            )
            if not _scope_satisfied(required_scope, env_api_key.scopes):
                raise AuthError(f"Key missing required scope: {required_scope!r}")
            return env_api_key

        # Check registered keys
        api_key = self._keys.get(key_hash)
        if api_key is None:
            raise AuthError("Invalid API key")

        if not api_key.active:
            raise AuthError("API key has been revoked")

        # Check expiry (for deprecated/rotated keys past their grace period)
        if api_key.expires_at is not None and time.time() >= api_key.expires_at:
            raise AuthError("API key has expired (rotation grace period ended)")

        if not _scope_satisfied(required_scope, api_key.scopes):
            raise AuthError(f"Key missing required scope: {required_scope!r}")

        api_key.last_used_at = time.time()
        return api_key

    def check_request(self, key: str | None = None, scope: str | None = None) -> dict[str, Any]:
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

    def create_key_with_role(
        self,
        name: str,
        role: Role,
    ) -> tuple[ApiKey, str]:
        """Create an API key with role-derived scopes (Enterprise).

        Args:
            name: Human-readable label for this key.
            role: The role to assign (admin, engineer, operator).

        Returns:
            (ApiKey metadata, raw key string).
        """
        scopes = ROLE_SCOPES.get(role, set())
        api_key, raw_key = self.create_key(name, scopes=list(scopes))
        api_key.role = role.value
        return api_key, raw_key

    def get_key_role(self, key: str) -> str | None:
        """Return the role name for a key, or None if no role is assigned."""
        key_hash = self._hash_key(key)
        api_key = self._keys.get(key_hash)
        if api_key is None:
            return None
        return api_key.role


def resolve_role_scopes(role_name: str) -> set[str]:
    """Resolve a role name string to its scope set.

    Args:
        role_name: One of "admin", "engineer", "operator".

    Returns:
        Set of scope strings. Empty set if role is unknown.
    """
    try:
        role = Role(role_name)
    except ValueError:
        return set()
    return ROLE_SCOPES.get(role, set())
