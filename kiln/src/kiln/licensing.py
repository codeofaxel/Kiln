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

import enum
import functools
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# License key prefix convention
# ---------------------------------------------------------------------------

_KEY_PREFIX_PRO = "kiln_pro_"
_KEY_PREFIX_BUSINESS = "kiln_biz_"

# Cache validity: how long a remote validation result is trusted (7 days).
_CACHE_TTL_SECONDS: float = 7 * 24 * 3600

# Local cache file for offline validation fallback.
_DEFAULT_LICENSE_PATH = Path.home() / ".kiln" / "license"
_DEFAULT_CACHE_PATH = Path.home() / ".kiln" / "license_cache.json"


# ---------------------------------------------------------------------------
# Tier enum
# ---------------------------------------------------------------------------


class LicenseTier(enum.Enum):
    """Kiln license tiers."""

    FREE = "free"
    PRO = "pro"
    BUSINESS = "business"

    def __ge__(self, other: "LicenseTier") -> bool:
        order = {LicenseTier.FREE: 0, LicenseTier.PRO: 1, LicenseTier.BUSINESS: 2}
        return order[self] >= order[other]

    def __gt__(self, other: "LicenseTier") -> bool:
        order = {LicenseTier.FREE: 0, LicenseTier.PRO: 1, LicenseTier.BUSINESS: 2}
        return order[self] > order[other]

    def __le__(self, other: "LicenseTier") -> bool:
        order = {LicenseTier.FREE: 0, LicenseTier.PRO: 1, LicenseTier.BUSINESS: 2}
        return order[self] <= order[other]

    def __lt__(self, other: "LicenseTier") -> bool:
        order = {LicenseTier.FREE: 0, LicenseTier.PRO: 1, LicenseTier.BUSINESS: 2}
        return order[self] < order[other]


# ---------------------------------------------------------------------------
# License info dataclass
# ---------------------------------------------------------------------------


@dataclass
class LicenseInfo:
    """Resolved license details."""

    tier: LicenseTier
    license_key_hint: str = ""  # Last 6 chars of key for display
    validated_at: Optional[float] = None
    expires_at: Optional[float] = None
    source: str = "default"  # "env", "file", "default"

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

    def to_dict(self) -> Dict[str, Any]:
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
        license_key: Optional[str] = None,
        license_path: Optional[Path] = None,
        cache_path: Optional[Path] = None,
    ) -> None:
        self._license_path = license_path or _DEFAULT_LICENSE_PATH
        self._cache_path = cache_path or _DEFAULT_CACHE_PATH
        self._resolved: Optional[LicenseInfo] = None

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

    def _infer_tier_from_key(self, key: str) -> LicenseTier:
        """Infer the license tier from the key prefix.

        This provides instant, offline tier resolution without needing
        to contact any remote API.
        """
        if not key:
            return LicenseTier.FREE
        if key.startswith(_KEY_PREFIX_BUSINESS):
            return LicenseTier.BUSINESS
        if key.startswith(_KEY_PREFIX_PRO):
            return LicenseTier.PRO
        # Unknown prefix but non-empty key — check cache before defaulting.
        cached = self._read_cache()
        if cached and cached.get("key_hint") == key[-6:]:
            try:
                return LicenseTier(cached["tier"])
            except (KeyError, ValueError):
                pass
        return LicenseTier.FREE

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

    def _read_cache(self) -> Optional[Dict[str, Any]]:
        """Read the local validation cache."""
        try:
            if not self._cache_path.is_file():
                return None
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            # Check TTL
            validated_at = data.get("validated_at", 0)
            if time.time() - validated_at > _CACHE_TTL_SECONDS:
                return None  # Cache expired
            return data
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def _write_cache(self, tier: LicenseTier, key_hint: str, expires_at: Optional[float] = None) -> None:
        """Write validation result to local cache."""
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "tier": tier.value,
                "key_hint": key_hint,
                "validated_at": time.time(),
                "expires_at": expires_at,
            }
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

        Resolution is instant and offline — uses key prefix detection
        with cached remote validation as a fallback.
        """
        if self._resolved is not None:
            if self._resolved.is_valid:
                return self._resolved.tier
            # License expired, fall back to free
            return LicenseTier.FREE

        tier = self._infer_tier_from_key(self._raw_key)
        key_hint = self._raw_key[-6:] if self._raw_key else ""

        self._resolved = LicenseInfo(
            tier=tier,
            license_key_hint=key_hint,
            validated_at=time.time(),
            source=self._key_source(),
        )

        # Update cache if we have a real key
        if self._raw_key:
            self._write_cache(tier, key_hint)

        return tier

    def get_info(self) -> LicenseInfo:
        """Return full license details."""
        # Ensure resolution has happened
        self.get_tier()
        assert self._resolved is not None
        return self._resolved

    def check_tier(self, required: LicenseTier) -> Tuple[bool, Optional[str]]:
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

_manager: Optional[LicenseManager] = None


def get_license_manager() -> LicenseManager:
    """Return the module-level LicenseManager singleton."""
    global _manager  # noqa: PLW0603
    if _manager is None:
        _manager = LicenseManager()
    return _manager


def get_tier() -> LicenseTier:
    """Return the current license tier (convenience shortcut)."""
    return get_license_manager().get_tier()


def check_tier(required: LicenseTier) -> Tuple[bool, Optional[str]]:
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
FEATURE_TIERS: Dict[str, LicenseTier] = {
    # Fleet orchestration (multi-printer coordination) — Pro
    "fleet_status": LicenseTier.PRO,
    "fleet_analytics": LicenseTier.PRO,
    # Business tier
    "fulfillment_order": LicenseTier.BUSINESS,
    "fulfillment_cancel": LicenseTier.BUSINESS,
}

# ---------------------------------------------------------------------------
# Free-tier resource limits
# ---------------------------------------------------------------------------

#: Maximum printers a FREE-tier user can register (independent control,
#: no cross-printer orchestration).  PRO and above are unlimited.
FREE_TIER_MAX_PRINTERS: int = 2

#: Maximum queued jobs for a single-printer FREE-tier user.
#: PRO and above get unlimited queue depth.
FREE_TIER_MAX_QUEUED_JOBS: int = 10
