"""Decoration quota tracking for free-tier users.

Free-tier users get 3 decorations per calendar month.  When kiln-pro is
installed and the user has a Pro (or higher) license, the quota is bypassed
and decorations are unlimited.

The quota is enforced in public Kiln — NOT in kiln-pro — so that free users
who don't have kiln-pro installed are still subject to the limit.  kiln-pro's
role is to *unlock* the limit, not enforce it.

Usage::

    from kiln.decoration_quota import check_decoration_quota

    ok, err = check_decoration_quota()   # checks + increments
    if not ok:
        return err  # DECORATION_QUOTA_EXCEEDED error dict

    status = decoration_quota_status()   # {"used": 1, "limit": 3, ...}
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_UPGRADE_URL = "https://kiln3d.com/pricing"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Free-tier decoration limit per calendar month.
FREE_TIER_DECORATION_LIMIT: int = 3

#: Path to the local quota tracking file.
DEFAULT_QUOTA_PATH: Path = Path.home() / ".kiln" / "decoration_usage.json"


# ---------------------------------------------------------------------------
# Quota status dataclass
# ---------------------------------------------------------------------------


@dataclass
class QuotaStatus:
    """Current decoration quota status."""

    used: int
    limit: int  # 0 means unlimited
    remaining: int  # -1 means unlimited
    tier: str
    month: str  # "YYYY-MM"
    unlimited: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "used": self.used,
            "limit": self.limit if not self.unlimited else "unlimited",
            "remaining": self.remaining if not self.unlimited else "unlimited",
            "tier": self.tier,
            "month": self.month,
            "unlimited": self.unlimited,
        }


# ---------------------------------------------------------------------------
# Quota tracker
# ---------------------------------------------------------------------------


class DecorationQuota:
    """Thread-safe decoration quota tracker.

    Reads/writes a simple JSON file at ``~/.kiln/decoration_usage.json``
    to track per-month decoration count.  License tier is resolved by
    trying to import kiln-pro; if absent, the user is on the free tier.

    Parameters:
        quota_path: Override the default quota file path (useful for tests).
    """

    def __init__(self, quota_path: Path | None = None) -> None:
        self._path = quota_path or DEFAULT_QUOTA_PATH
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Tier resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _get_tier() -> str:
        """Return the current license tier as a lowercase string."""
        try:
            from kiln.licensing import get_tier
            tier = get_tier()
            return tier.value if hasattr(tier, "value") else str(tier)
        except Exception:
            return "free"

    @staticmethod
    def _is_unlimited(tier: str) -> bool:
        """Return True if the tier has unlimited decorations."""
        return tier in ("pro", "business", "enterprise", "founder")

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    @staticmethod
    def _current_month() -> str:
        """Return the current month as ``YYYY-MM``."""
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _read(self) -> dict[str, Any]:
        """Read the quota file, returning defaults if missing/corrupt."""
        try:
            if self._path.is_file():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "month" in data and "count" in data:
                    return data
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.debug("Could not read decoration quota file: %s", exc)
        return {"month": self._current_month(), "count": 0}

    def _write(self, data: dict[str, Any]) -> None:
        """Write quota data to the JSON file."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Could not write decoration quota file: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_status(self) -> QuotaStatus:
        """Return the current decoration quota status."""
        tier = self._get_tier()
        unlimited = self._is_unlimited(tier)
        month = self._current_month()

        with self._lock:
            data = self._read()
            if data.get("month") != month:
                used = 0
            else:
                used = int(data.get("count", 0))

        limit = 0 if unlimited else FREE_TIER_DECORATION_LIMIT
        remaining = -1 if unlimited else max(0, limit - used)

        return QuotaStatus(
            used=used,
            limit=limit,
            remaining=remaining,
            tier=tier,
            month=month,
            unlimited=unlimited,
        )

    def check_and_increment(self) -> tuple[bool, dict[str, Any] | None]:
        """Atomically check quota and increment the counter if allowed.

        Returns:
            ``(True, None)`` if allowed (counter incremented).
            ``(False, error_dict)`` if the quota would be exceeded
            (counter NOT incremented).
        """
        tier = self._get_tier()
        unlimited = self._is_unlimited(tier)
        month = self._current_month()

        with self._lock:
            data = self._read()

            if data.get("month") != month:
                data = {"month": month, "count": 0}

            current_count = int(data.get("count", 0))

            if not unlimited and current_count >= FREE_TIER_DECORATION_LIMIT:
                status = QuotaStatus(
                    used=current_count,
                    limit=FREE_TIER_DECORATION_LIMIT,
                    remaining=0,
                    tier=tier,
                    month=month,
                    unlimited=False,
                )
                return False, _quota_exceeded_error(status)

            data["count"] = current_count + 1
            data["month"] = month
            self._write(data)

        return True, None

    def increment(self) -> None:
        """Unconditionally increment the decoration counter."""
        month = self._current_month()
        with self._lock:
            data = self._read()
            if data.get("month") != month:
                data = {"month": month, "count": 0}
            data["count"] = int(data.get("count", 0)) + 1
            self._write(data)


# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------


def _quota_exceeded_error(status: QuotaStatus) -> dict[str, Any]:
    """Build the standard quota-exceeded error dict."""
    return {
        "status": "error",
        "error": (
            f"Free tier limit reached ({status.used}/{FREE_TIER_DECORATION_LIMIT} "
            f"decorations this month). Upgrade to Pro for unlimited decorations."
        ),
        "code": "DECORATION_QUOTA_EXCEEDED",
        "quota": status.to_dict(),
        "upgrade_url": _UPGRADE_URL,
    }


# ---------------------------------------------------------------------------
# Module-level singleton + convenience functions
# ---------------------------------------------------------------------------

_quota: DecorationQuota | None = None


def get_decoration_quota() -> DecorationQuota:
    """Return the module-level DecorationQuota singleton."""
    global _quota  # noqa: PLW0603
    if _quota is None:
        _quota = DecorationQuota()
    return _quota


def check_decoration_quota() -> tuple[bool, dict[str, Any] | None]:
    """Check and increment the decoration quota.

    Returns:
        ``(True, None)`` if the decoration is allowed.
        ``(False, error_dict)`` if quota exceeded.
    """
    return get_decoration_quota().check_and_increment()


def decoration_quota_status() -> dict[str, Any]:
    """Return the current quota status as a dict (for MCP tool responses)."""
    return get_decoration_quota().get_status().to_dict()
