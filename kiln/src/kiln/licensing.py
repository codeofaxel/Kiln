"""License tier management for Kiln.

Provides a simple, offline-first licensing system that gates premium
features (fleet management, job queue, scheduler, advanced analytics)
behind a Pro or Business tier.

Free tier includes all single-printer control, safety checks, and slicer
integration with no restrictions.

License resolution order (highest priority first):
    1. ``KILN_LICENSE_KEY`` environment variable
    2. ``~/.kiln/license`` file
    3. Defaults to ``FREE``

The license key is validated locally first (format check + optional
cached validation result).  When a remote validation endpoint is
configured, the key is verified against the Kiln API with results
cached locally to ensure offline operation is never blocked.

Example::

    from kiln.licensing import LicenseManager, LicenseTier

    mgr = LicenseManager()
    mgr.get_tier()          # → LicenseTier.FREE
    mgr.check_tier(LicenseTier.PRO)  # → (False, "...")

    # With a valid license key:
    mgr = LicenseManager(license_key="kiln_pro_abc123...")
    mgr.get_tier()          # → LicenseTier.PRO
"""

from __future__ import annotations

import base64
import enum
import functools
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import time
import warnings
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# License key prefix convention
# ---------------------------------------------------------------------------

_KEY_PREFIX_PRO = "kiln_pro_"
_KEY_PREFIX_BUSINESS = "kiln_biz_"
_KEY_PREFIX_ENTERPRISE = "kiln_ent_"
_KEY_PREFIX_V2 = "kiln_v2_"

# Built-in public verify key for v2 licenses (Ed25519).
# This key is safe to publish; only the matching private key can mint licenses.
_V2_DEFAULT_KEY_ID = "k1"
_V2_DEFAULT_VERIFY_KEYS: dict[str, str] = {
    _V2_DEFAULT_KEY_ID: "tu4SayAZ4W2MJ4w8ZrMjhgkLn7LY3aB5yxilwsE_4aQ",
}
_V2_VERIFY_KEYS_ENV = "KILN_LICENSE_VERIFY_KEYS_JSON"
_V2_PRIVATE_KEY_ENV = "KILN_LICENSE_SIGNING_PRIVATE_KEY"

# Cache validity: how long a remote validation result is trusted (7 days).
_CACHE_TTL_SECONDS: float = 7 * 24 * 3600

# Local cache file for offline validation fallback.
_DEFAULT_LICENSE_PATH = Path.home() / ".kiln" / "license"
_DEFAULT_CACHE_PATH = Path.home() / ".kiln" / "license_cache.json"

# ---------------------------------------------------------------------------
# Cryptographic signature validation
# ---------------------------------------------------------------------------

# Offline mode bypass: if set to "1", allow prefix-based keys even when signature fails.
_OFFLINE_MODE_ENV_VAR = "KILN_LICENSE_OFFLINE"


def _decode_b64_flexible(value: str) -> bytes:
    """Decode standard or url-safe base64 with optional missing padding."""
    padded = value + "=" * ((4 - len(value) % 4) % 4)
    try:
        return base64.b64decode(padded)
    except Exception:
        return base64.urlsafe_b64decode(padded)


def _encode_b64_no_pad(raw: bytes) -> str:
    """Encode bytes as standard base64 without trailing padding."""
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def _load_v2_verify_keys() -> dict[str, Ed25519PublicKey]:
    """Load v2 Ed25519 verify keys keyed by key id (kid)."""
    raw_map = dict(_V2_DEFAULT_VERIFY_KEYS)
    env_json = os.environ.get(_V2_VERIFY_KEYS_ENV, "").strip()
    if env_json:
        try:
            parsed = json.loads(env_json)
            if isinstance(parsed, dict):
                for kid, key in parsed.items():
                    if isinstance(kid, str) and isinstance(key, str) and kid.strip() and key.strip():
                        raw_map[kid.strip()] = key.strip()
        except Exception as exc:
            logger.warning("Invalid %s value: %s", _V2_VERIFY_KEYS_ENV, exc)

    out: dict[str, Ed25519PublicKey] = {}
    for kid, key_raw in raw_map.items():
        try:
            key_bytes = _decode_b64_flexible(key_raw)
            out[kid] = Ed25519PublicKey.from_public_bytes(key_bytes)
        except Exception as exc:
            logger.warning("Failed to load v2 verify key %s: %s", kid, exc)
    return out


def _load_v2_private_key(key_value: str) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from PEM, base64, base64url, or hex."""
    value = key_value.strip()
    if not value:
        raise ValueError("Empty signing private key")

    if "BEGIN" in value:
        key = serialization.load_pem_private_key(value.encode("utf-8"), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("PEM key is not an Ed25519 private key")
        return key

    try:
        raw = bytes.fromhex(value)
    except ValueError:
        raw = _decode_b64_flexible(value)
    if len(raw) != 32:
        raise ValueError("Ed25519 private key must be 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(raw)


# ---------------------------------------------------------------------------
# Tier enum
# ---------------------------------------------------------------------------


class LicenseTier(enum.Enum):
    """Kiln license tiers."""

    FREE = "free"
    PRO = "pro"
    BUSINESS = "business"
    ENTERPRISE = "enterprise"

    def __ge__(self, other: LicenseTier) -> bool:
        order = {LicenseTier.FREE: 0, LicenseTier.PRO: 1, LicenseTier.BUSINESS: 2, LicenseTier.ENTERPRISE: 3}
        return order[self] >= order[other]

    def __gt__(self, other: LicenseTier) -> bool:
        order = {LicenseTier.FREE: 0, LicenseTier.PRO: 1, LicenseTier.BUSINESS: 2, LicenseTier.ENTERPRISE: 3}
        return order[self] > order[other]

    def __le__(self, other: LicenseTier) -> bool:
        order = {LicenseTier.FREE: 0, LicenseTier.PRO: 1, LicenseTier.BUSINESS: 2, LicenseTier.ENTERPRISE: 3}
        return order[self] <= order[other]

    def __lt__(self, other: LicenseTier) -> bool:
        order = {LicenseTier.FREE: 0, LicenseTier.PRO: 1, LicenseTier.BUSINESS: 2, LicenseTier.ENTERPRISE: 3}
        return order[self] < order[other]


# ---------------------------------------------------------------------------
# License info dataclass
# ---------------------------------------------------------------------------


@dataclass
class LicenseInfo:
    """Resolved license details."""

    tier: LicenseTier
    license_key_hint: str = ""  # Last 6 chars of key for display
    validated_at: float | None = None
    expires_at: float | None = None
    source: str = "default"  # "env", "file", "default"
    email: str = ""  # Email from signed payload (empty for legacy/unsigned keys)

    @property
    def is_expired(self) -> bool:
        """Whether the license has passed its expiration date."""
        if self.expires_at is None:
            return False
        return time.time() >= self.expires_at

    @property
    def is_valid(self) -> bool:
        """Whether the license is currently valid (not expired)."""
        return not self.is_expired

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["tier"] = self.tier.value
        data["is_expired"] = self.is_expired
        data["is_valid"] = self.is_valid
        return data


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LicenseError(Exception):
    """Base class for licensing errors."""

    pass


class TierRequiredError(LicenseError):
    """Raised when a feature requires a higher tier."""

    def __init__(self, feature: str, required_tier: LicenseTier) -> None:
        self.feature = feature
        self.required_tier = required_tier
        super().__init__(
            f"{feature} requires a Kiln {required_tier.value.title()} license. "
            f"Upgrade at https://kiln3d.com/pro or run 'kiln upgrade'."
        )


# ---------------------------------------------------------------------------
# License Manager
# ---------------------------------------------------------------------------


class LicenseManager:
    """Manages license key resolution and tier checking.

    Offline-first: never blocks printer operations if the validation
    API is unreachable.  Uses local key format detection + cached
    remote validation results.
    """

    def __init__(
        self,
        license_key: str | None = None,
        license_path: Path | None = None,
        cache_path: Path | None = None,
    ) -> None:
        self._license_path = license_path or _DEFAULT_LICENSE_PATH
        self._cache_path = cache_path or _DEFAULT_CACHE_PATH
        self._resolved: LicenseInfo | None = None

        # Resolve the license key from explicit arg, env, or file.
        self._raw_key = license_key or self._resolve_key()

    def _resolve_key(self) -> str:
        """Resolve the license key from env var or file."""
        # 1. Environment variable (highest priority)
        env_key = os.environ.get("KILN_LICENSE_KEY", "").strip()
        if env_key:
            return env_key

        # 2. License file
        try:
            if self._license_path.is_file():
                key = self._license_path.read_text(encoding="utf-8").strip()
                if key:
                    return key
        except OSError as exc:
            logger.debug("Could not read license file %s: %s", self._license_path, exc)

        return ""

    def _validate_key_signature(self, key: str) -> dict[str, Any] | None:
        """Validate the cryptographic signature of a license key.

        Expected format: kiln_{tier}_{payload}_{signature}
        Where:
            - tier = "pro" or "biz"
            - payload = base64url-encoded JSON with {"tier", "email", "issued_at", "expires_at"}
            - signature = HMAC-SHA256 of payload using verification key

        Returns:
            The decoded payload dict if valid, None otherwise.
        """
        if not key or not key.startswith("kiln_"):
            return None

        parts = key.split("_", 3)
        if len(parts) < 4:
            # Not a signed key format
            return None

        tier_part = parts[1]  # "pro" or "biz"
        payload_b64 = parts[2]
        signature_b64 = parts[3]

        if tier_part == "v2":
            return self._validate_key_signature_v2(payload_b64, signature_b64)

        # Get verification key — prefer KILN_LICENSE_SIGNING_SECRET, fall back
        # to legacy KILN_LICENSE_PUBLIC_KEY.
        verification_key = os.environ.get("KILN_LICENSE_SIGNING_SECRET", "").strip()
        if not verification_key:
            verification_key = os.environ.get("KILN_LICENSE_PUBLIC_KEY", "").strip()
        if not verification_key:
            logger.debug("KILN_LICENSE_SIGNING_SECRET not set — signature verification unavailable")
            return None

        try:
            # Decode payload
            # Use standard base64 first, fallback to urlsafe
            try:
                payload_bytes = base64.b64decode(payload_b64)
            except Exception:
                payload_bytes = base64.urlsafe_b64decode(payload_b64 + "==")  # Add padding

            payload = json.loads(payload_bytes.decode("utf-8"))

            # Verify signature
            try:
                signature = base64.b64decode(signature_b64)
            except Exception:
                signature = base64.urlsafe_b64decode(signature_b64 + "==")

            expected_signature = hmac.new(
                verification_key.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256
            ).digest()

            if not hmac.compare_digest(signature, expected_signature):
                logger.warning("License key signature verification failed")
                return None

            # Check expiration
            expires_at = payload.get("expires_at")
            if expires_at and time.time() >= expires_at:
                logger.warning("License key has expired")
                return None

            # Verify tier matches
            payload_tier = payload.get("tier", "").lower()
            if tier_part == "pro" and payload_tier != "pro":
                logger.warning("License key tier mismatch: prefix=%s payload=%s", tier_part, payload_tier)
                return None
            if tier_part == "biz" and payload_tier != "business":
                logger.warning("License key tier mismatch: prefix=%s payload=%s", tier_part, payload_tier)
                return None
            if tier_part == "ent" and payload_tier != "enterprise":
                logger.warning("License key tier mismatch: prefix=%s payload=%s", tier_part, payload_tier)
                return None

            return payload

        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
            logger.debug("License key signature validation failed: %s", exc)
            return None

    def _validate_key_signature_v2(self, payload_b64: str, signature_b64: str) -> dict[str, Any] | None:
        """Validate v2 Ed25519-signed license payloads.

        v2 format:
            ``kiln_v2_{payload_b64}_{signature_b64}``

        Payload fields:
            - ``tier``: free/pro/business/enterprise
            - ``email``: account email
            - ``issued_at``: unix timestamp
            - ``expires_at``: unix timestamp
            - ``jti``: token id
            - ``kid``: verify key id
        """
        verify_keys = _load_v2_verify_keys()
        if not verify_keys:
            logger.warning("No v2 verify keys available")
            return None

        try:
            payload_bytes = _decode_b64_flexible(payload_b64)
            payload = json.loads(payload_bytes.decode("utf-8"))
            if not isinstance(payload, dict):
                return None

            kid = str(payload.get("kid") or _V2_DEFAULT_KEY_ID)
            verifier = verify_keys.get(kid)
            if verifier is None:
                logger.warning("Unknown v2 key id: %s", kid)
                return None

            signature = _decode_b64_flexible(signature_b64)
            verifier.verify(signature, payload_b64.encode("ascii"))

            expires_at = payload.get("expires_at")
            if expires_at is not None and time.time() >= float(expires_at):
                logger.warning("License key has expired")
                return None

            tier = str(payload.get("tier", "")).lower()
            if tier not in {"free", "pro", "business", "enterprise"}:
                logger.warning("Invalid v2 license tier: %s", tier)
                return None

            return payload
        except (InvalidSignature, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("v2 license validation failed: %s", exc)
            return None

    def _infer_tier_from_key(self, key: str) -> tuple[LicenseTier, float | None, str]:
        """Infer the license tier from the key, validating signature if present.

        This provides instant, offline tier resolution without needing
        to contact any remote API.

        Returns:
            (tier, expires_at, email) tuple. expires_at is None for legacy
            keys or free tier. email is empty for unsigned/legacy keys.
        """
        if not key:
            return LicenseTier.FREE, None, ""

        # Try cryptographic validation first
        payload = self._validate_key_signature(key)
        if payload:
            tier_str = payload.get("tier", "").lower()
            email = payload.get("email", "")
            if tier_str == "business":
                return LicenseTier.BUSINESS, payload.get("expires_at"), email
            if tier_str == "enterprise":
                return LicenseTier.ENTERPRISE, payload.get("expires_at"), email
            if tier_str == "pro":
                return LicenseTier.PRO, payload.get("expires_at"), email
            logger.warning("Unknown tier in validated payload: %s", tier_str)

        # Signature validation failed — check offline cache before rejecting.
        offline_mode = os.environ.get(_OFFLINE_MODE_ENV_VAR, "0") == "1"

        if not offline_mode:
            # Online mode: signature is mandatory for non-free tiers.
            if key.startswith((_KEY_PREFIX_PRO, _KEY_PREFIX_BUSINESS, _KEY_PREFIX_ENTERPRISE, _KEY_PREFIX_V2)):
                logger.warning(
                    "License key signature validation failed. "
                    "Set KILN_LICENSE_OFFLINE=1 to allow cached offline validation, "
                    "or upgrade to a properly signed key. Defaulting to FREE tier for security."
                )
            return LicenseTier.FREE, None, ""

        # Offline mode: only accept keys that have a valid cached validation.
        # Never accept prefix-only — a previous successful validation must exist.
        cached = self._read_cache()
        if cached and cached.get("key_hint") == key[-6:]:
            try:
                cached_tier = LicenseTier(cached["tier"])
                logger.info(
                    "Offline mode: using cached validation for key hint ...%s (tier=%s)",
                    key[-6:],
                    cached_tier.value,
                )
                return cached_tier, cached.get("expires_at"), ""
            except (KeyError, ValueError):
                pass

        logger.warning(
            "License key signature validation failed and no cached validation found. "
            "KILN_LICENSE_OFFLINE=1 requires a previous successful online validation. "
            "Defaulting to FREE tier for security."
        )
        return LicenseTier.FREE, None, ""

    def _key_source(self) -> str:
        """Determine where the license key came from."""
        env_key = os.environ.get("KILN_LICENSE_KEY", "").strip()
        if env_key:
            return "env"
        try:
            if self._license_path.is_file():
                file_key = self._license_path.read_text(encoding="utf-8").strip()
                if file_key:
                    return "file"
        except OSError:
            pass
        return "default"

    # ------------------------------------------------------------------
    # Cache (offline fallback)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_cache_signing_key() -> str:
        """Return the key used to HMAC-sign the license cache.

        Uses ``KILN_LICENSE_SIGNING_SECRET`` (preferred) falling back to
        ``KILN_LICENSE_PUBLIC_KEY`` for backwards compatibility.
        """
        key = os.environ.get("KILN_LICENSE_SIGNING_SECRET", "").strip()
        if not key:
            key = os.environ.get("KILN_LICENSE_PUBLIC_KEY", "").strip()
        return key

    def _read_cache(self) -> dict[str, Any] | None:
        """Read and verify the HMAC-signed local validation cache."""
        try:
            if not self._cache_path.is_file():
                return None
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None

            # Verify HMAC integrity before trusting cache contents.
            signing_key = self._get_cache_signing_key()
            if signing_key:
                stored_mac = data.pop("hmac", None)
                if not stored_mac:
                    logger.warning("License cache missing HMAC — discarding")
                    return None
                json_bytes = json.dumps(data, sort_keys=True).encode("utf-8")
                expected_mac = hmac.new(
                    signing_key.encode("utf-8"), json_bytes, hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(stored_mac, expected_mac):
                    logger.warning("License cache HMAC mismatch — discarding")
                    return None

            # Check TTL
            validated_at = data.get("validated_at", 0)
            if time.time() - validated_at > _CACHE_TTL_SECONDS:
                return None  # Cache expired
            return data
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def _write_cache(self, tier: LicenseTier, key_hint: str, expires_at: float | None = None) -> None:
        """Write HMAC-signed validation result to local cache."""
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            data: dict[str, Any] = {
                "tier": tier.value,
                "key_hint": key_hint,
                "validated_at": time.time(),
                "expires_at": expires_at,
            }
            # Sign the cache contents so tampering is detectable.
            signing_key = self._get_cache_signing_key()
            if signing_key:
                json_bytes = json.dumps(data, sort_keys=True).encode("utf-8")
                data["hmac"] = hmac.new(
                    signing_key.encode("utf-8"), json_bytes, hashlib.sha256
                ).hexdigest()
            self._cache_path.write_text(json.dumps(data), encoding="utf-8")
            # Secure permissions
            if sys.platform != "win32":
                self._cache_path.chmod(0o600)
        except OSError as exc:
            logger.debug("Could not write license cache: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_tier(self) -> LicenseTier:
        """Return the current license tier.

        Resolution is instant and offline — uses cryptographic signature
        validation with cached offline fallback.
        """
        # Emit deprecation warning if only the legacy env var name is set.
        if (
            os.environ.get("KILN_LICENSE_PUBLIC_KEY", "").strip()
            and not os.environ.get("KILN_LICENSE_SIGNING_SECRET", "").strip()
        ):
            warnings.warn(
                "KILN_LICENSE_PUBLIC_KEY is deprecated — use KILN_LICENSE_SIGNING_SECRET instead. "
                "KILN_LICENSE_PUBLIC_KEY will be removed in a future release.",
                DeprecationWarning,
                stacklevel=2,
            )

        if self._resolved is not None:
            if self._resolved.is_valid:
                return self._resolved.tier
            # License expired, fall back to free
            return LicenseTier.FREE

        tier, expires_at, email = self._infer_tier_from_key(self._raw_key)
        key_hint = self._raw_key[-6:] if self._raw_key else ""

        self._resolved = LicenseInfo(
            tier=tier,
            license_key_hint=key_hint,
            validated_at=time.time(),
            expires_at=expires_at,
            source=self._key_source(),
            email=email,
        )

        # Update cache if we have a real key
        if self._raw_key:
            self._write_cache(tier, key_hint, expires_at)

        return tier

    def get_info(self) -> LicenseInfo:
        """Return full license details."""
        # Ensure resolution has happened
        self.get_tier()
        assert self._resolved is not None
        return self._resolved

    def check_tier(self, required: LicenseTier) -> tuple[bool, str | None]:
        """Check if the current license meets the required tier.

        Returns:
            ``(True, None)`` if the tier is sufficient.
            ``(False, error_message)`` if upgrade is needed.
        """
        current = self.get_tier()
        if current >= required:
            return True, None
        return False, (
            f"This feature requires a Kiln {required.value.title()} license. "
            f"You're on the {current.value.title()} tier. "
            f"Upgrade at https://kiln3d.com/pro or run 'kiln upgrade'."
        )

    def activate_license(self, key: str) -> LicenseInfo:
        """Activate a license key by saving it to the license file.

        Args:
            key: The raw license key string.

        Returns:
            The resolved license info after activation.
        """
        # Save to file
        self._license_path.parent.mkdir(parents=True, exist_ok=True)
        self._license_path.write_text(key.strip(), encoding="utf-8")

        # Secure permissions
        if sys.platform != "win32":
            try:
                self._license_path.chmod(0o600)
                self._license_path.parent.chmod(0o700)
            except OSError:
                pass

        # Re-resolve
        self._raw_key = key.strip()
        self._resolved = None
        self.get_tier()

        logger.info(
            "License activated: tier=%s source=file",
            self._resolved.tier.value,
        )
        return self._resolved

    def deactivate_license(self) -> None:
        """Remove the local license key and cache."""
        try:
            if self._license_path.is_file():
                self._license_path.unlink()
        except OSError:
            pass
        try:
            if self._cache_path.is_file():
                self._cache_path.unlink()
        except OSError:
            pass
        self._raw_key = ""
        self._resolved = None


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

_manager: LicenseManager | None = None


def get_license_manager() -> LicenseManager:
    """Return the module-level LicenseManager singleton."""
    global _manager  # noqa: PLW0603
    if _manager is None:
        _manager = LicenseManager()
    return _manager


def get_tier() -> LicenseTier:
    """Return the current license tier (convenience shortcut)."""
    return get_license_manager().get_tier()


def check_tier(required: LicenseTier) -> tuple[bool, str | None]:
    """Check if the current tier meets the requirement (convenience shortcut)."""
    return get_license_manager().check_tier(required)


# ---------------------------------------------------------------------------
# Decorator for gating MCP tools and CLI commands
# ---------------------------------------------------------------------------


def requires_tier(tier: LicenseTier) -> Callable:
    """Decorator that gates a function behind a license tier.

    For MCP tools (functions returning dicts), returns an error dict
    when the tier check fails.  For CLI commands, raises TierRequiredError.

    Usage::

        @mcp.tool()
        @requires_tier(LicenseTier.PRO)
        def fleet_status() -> dict:
            ...

    The decorated function's name, docstring, and signature are preserved.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            ok, message = check_tier(tier)
            if not ok:
                # Check if this looks like an MCP tool (returns dict)
                # by checking the return annotation or just returning the
                # standard error dict.
                return {
                    "success": False,
                    "error": message,
                    "code": "LICENSE_REQUIRED",
                    "required_tier": tier.value,
                    "upgrade_url": "https://kiln3d.com/pro",
                }
            return func(*args, **kwargs)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Feature-to-tier mapping
# ---------------------------------------------------------------------------

# Which features require which tier.  Used by the decorator and by
# documentation/help text generation.
FEATURE_TIERS: dict[str, LicenseTier] = {
    # Fleet orchestration (multi-printer coordination) — Pro
    "fleet_status": LicenseTier.PRO,
    "fleet_analytics": LicenseTier.PRO,
    # Business tier
    "fulfillment_order": LicenseTier.BUSINESS,
    "fulfillment_cancel": LicenseTier.BUSINESS,
    # Enterprise tier
    "dedicated_mcp_server": LicenseTier.ENTERPRISE,
    "sso_authentication": LicenseTier.ENTERPRISE,
    "audit_trail_export": LicenseTier.ENTERPRISE,
    "role_based_access": LicenseTier.ENTERPRISE,
    "lockable_safety_profiles": LicenseTier.ENTERPRISE,
    "on_prem_deployment": LicenseTier.ENTERPRISE,
}

# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------


def generate_license_key(
    tier: LicenseTier,
    email: str,
    *,
    signing_key: str | None = None,
    ttl_seconds: int = 365 * 24 * 3600,
) -> str:
    """Generate a cryptographically signed license key.

    :param tier: License tier (FREE, PRO, or BUSINESS).
    :param email: Buyer's email address (or registrant for FREE).
    :param signing_key: HMAC secret. Falls back to ``KILN_LICENSE_PUBLIC_KEY``.
    :param ttl_seconds: Key validity duration in seconds (default 1 year).
    :returns: Signed key string ``kiln_{prefix}_{payload_b64}_{signature_b64}``.
    :raises ValueError: If signing key is missing.
    """
    if signing_key is None:
        signing_key = os.environ.get("KILN_LICENSE_SIGNING_SECRET", "").strip()
        if not signing_key:
            signing_key = os.environ.get("KILN_LICENSE_PUBLIC_KEY", "").strip()
    if not signing_key:
        raise ValueError("Signing key is required: pass signing_key or set KILN_LICENSE_SIGNING_SECRET")

    now = time.time()
    payload = {
        "tier": tier.value,
        "email": email,
        "issued_at": now,
        "expires_at": now + ttl_seconds,
    }

    payload_b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).rstrip(b"=").decode("ascii")

    signature = hmac.new(
        signing_key.encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    signature_b64 = base64.b64encode(signature).rstrip(b"=").decode("ascii")

    _TIER_PREFIXES = {
        LicenseTier.FREE: "free",
        LicenseTier.PRO: "pro",
        LicenseTier.BUSINESS: "biz",
        LicenseTier.ENTERPRISE: "ent",
    }
    prefix = _TIER_PREFIXES[tier]
    return f"kiln_{prefix}_{payload_b64}_{signature_b64}"


def parse_license_claims(key: str) -> dict[str, Any] | None:
    """Parse claims from a signed license key without validating signature.

    Supports both legacy and v2 key formats. Intended for administrative
    workflows (issuance logs, display metadata) where signature validation
    happens separately via ``LicenseManager``.
    """
    if not key or not key.startswith("kiln_"):
        return None
    parts = key.split("_", 3)
    if len(parts) < 4:
        return None
    payload_b64 = parts[2]
    try:
        payload = json.loads(_decode_b64_flexible(payload_b64).decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def generate_license_key_v2(
    tier: LicenseTier,
    email: str,
    *,
    ttl_seconds: int = 30 * 24 * 3600,
    key_id: str = _V2_DEFAULT_KEY_ID,
    signing_private_key: str | None = None,
    features: list[str] | None = None,
    notes: str | None = None,
) -> str:
    """Generate a v2 Ed25519-signed license key.

    v2 keys are validated with a public key baked into the client, so
    users do not need access to signing secrets.
    """
    key_value = signing_private_key or os.environ.get(_V2_PRIVATE_KEY_ENV, "").strip()
    if not key_value:
        raise ValueError(
            f"Signing key is required: pass signing_private_key or set {_V2_PRIVATE_KEY_ENV}"
        )
    private_key = _load_v2_private_key(key_value)

    now = time.time()
    payload: dict[str, Any] = {
        "version": 2,
        "kid": key_id,
        "jti": secrets.token_hex(16),
        "tier": tier.value,
        "email": email,
        "issued_at": now,
        "expires_at": now + ttl_seconds,
    }
    if features:
        payload["features"] = sorted({str(f).strip() for f in features if str(f).strip()})
    if notes:
        payload["notes"] = notes

    payload_raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload_b64 = _encode_b64_no_pad(payload_raw)
    signature_b64 = _encode_b64_no_pad(private_key.sign(payload_b64.encode("ascii")))
    return f"{_KEY_PREFIX_V2}{payload_b64}_{signature_b64}"


# ---------------------------------------------------------------------------
# Free-tier resource limits
# ---------------------------------------------------------------------------

#: Maximum printers a FREE-tier user can register (independent control,
#: no cross-printer orchestration).  PRO and above are unlimited.
FREE_TIER_MAX_PRINTERS: int = 2

#: Maximum queued jobs for a single-printer FREE-tier user.
#: PRO and above get unlimited queue depth.
FREE_TIER_MAX_QUEUED_JOBS: int = 10

#: Maximum printers for a BUSINESS-tier user. Enterprise is unlimited.
BUSINESS_TIER_MAX_PRINTERS: int = 50

#: Number of team seats included in BUSINESS tier.
BUSINESS_TIER_MAX_SEATS: int = 5
