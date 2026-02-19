"""Uptime health monitoring for Enterprise tier.

Tracks health check results over time and calculates rolling uptime
percentages for SLA visibility. Integrates with the existing
``health_check()`` MCP tool infrastructure.

Data is persisted to ``~/.kiln/uptime.json`` and rotated to keep
only the last 30 days of history.

Usage::

    from kiln.uptime import UptimeTracker

    tracker = UptimeTracker()
    tracker.record_check(healthy=True)
    report = tracker.uptime_report()
    print(report["uptime_30d"])  # 99.95
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_UPTIME_DIR = Path.home() / ".kiln"
_UPTIME_FILE = _UPTIME_DIR / "uptime.json"

# Retention: keep 30 days of check history.
_RETENTION_SECONDS: float = 30 * 24 * 3600

# Check intervals for rolling windows.
_WINDOW_1H: float = 3600
_WINDOW_24H: float = 86400
_WINDOW_7D: float = 7 * 86400
_WINDOW_30D: float = 30 * 86400


@dataclass
class HealthCheck:
    """A single health check result.

    Attributes:
        timestamp: When the check was performed (Unix time).
        healthy: Whether the system was healthy.
        response_ms: Response time in milliseconds, or ``None``.
        details: Optional diagnostic info.
    """

    timestamp: float
    healthy: bool
    response_ms: float | None = None
    details: str | None = None


class UptimeTracker:
    """Tracks health check history and computes rolling uptime.

    Persists checks to ``~/.kiln/uptime.json``. Old entries beyond
    the retention window are pruned on save.
    """

    def __init__(self, *, data_file: Path | None = None) -> None:
        self._data_file = data_file or _UPTIME_FILE
        self._checks: list[HealthCheck] = []
        self._load()

    def _load(self) -> None:
        """Load check history from disk."""
        if not self._data_file.exists():
            return
        try:
            data = json.loads(self._data_file.read_text(encoding="utf-8"))
            for entry in data.get("checks", []):
                self._checks.append(HealthCheck(
                    timestamp=entry["timestamp"],
                    healthy=entry["healthy"],
                    response_ms=entry.get("response_ms"),
                    details=entry.get("details"),
                ))
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.error("Failed to load uptime data: %s", exc)

    def _save(self) -> None:
        """Persist check history, pruning old entries."""
        cutoff = time.time() - _RETENTION_SECONDS
        self._checks = [c for c in self._checks if c.timestamp >= cutoff]

        self._data_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "checks": [
                {
                    "timestamp": c.timestamp,
                    "healthy": c.healthy,
                    "response_ms": c.response_ms,
                    "details": c.details,
                }
                for c in self._checks
            ],
            "updated_at": time.time(),
        }
        self._data_file.write_text(json.dumps(data), encoding="utf-8")

    def record_check(
        self,
        *,
        healthy: bool,
        response_ms: float | None = None,
        details: str | None = None,
    ) -> HealthCheck:
        """Record a health check result.

        Args:
            healthy: Whether the system passed the health check.
            response_ms: Response time in milliseconds.
            details: Optional diagnostic message.

        Returns:
            The recorded :class:`HealthCheck`.
        """
        check = HealthCheck(
            timestamp=time.time(),
            healthy=healthy,
            response_ms=response_ms,
            details=details,
        )
        self._checks.append(check)
        self._save()
        return check

    def _uptime_for_window(self, window_seconds: float) -> float | None:
        """Calculate uptime percentage for a time window.

        Returns:
            Percentage (0-100) or ``None`` if no checks in the window.
        """
        cutoff = time.time() - window_seconds
        window_checks = [c for c in self._checks if c.timestamp >= cutoff]

        if not window_checks:
            return None

        healthy_count = sum(1 for c in window_checks if c.healthy)
        return round((healthy_count / len(window_checks)) * 100, 4)

    def _avg_response_ms(self, window_seconds: float) -> float | None:
        """Average response time in a window (only checks with response_ms)."""
        cutoff = time.time() - window_seconds
        times = [
            c.response_ms
            for c in self._checks
            if c.timestamp >= cutoff and c.response_ms is not None
        ]
        if not times:
            return None
        return round(sum(times) / len(times), 1)

    def uptime_report(self) -> dict[str, Any]:
        """Generate a comprehensive uptime report.

        Returns:
            Dict with rolling uptime percentages, check counts,
            average response times, and last check info.
        """
        now = time.time()

        # Rolling uptime for each window
        uptime_1h = self._uptime_for_window(_WINDOW_1H)
        uptime_24h = self._uptime_for_window(_WINDOW_24H)
        uptime_7d = self._uptime_for_window(_WINDOW_7D)
        uptime_30d = self._uptime_for_window(_WINDOW_30D)

        # Total checks in each window
        checks_24h = sum(1 for c in self._checks if c.timestamp >= now - _WINDOW_24H)
        checks_7d = sum(1 for c in self._checks if c.timestamp >= now - _WINDOW_7D)
        checks_30d = sum(1 for c in self._checks if c.timestamp >= now - _WINDOW_30D)

        # Last check
        last_check = None
        if self._checks:
            last = self._checks[-1]
            last_check = {
                "timestamp": last.timestamp,
                "healthy": last.healthy,
                "response_ms": last.response_ms,
                "details": last.details,
                "age_seconds": round(now - last.timestamp, 1),
            }

        # SLA status (99.9% threshold)
        sla_met = uptime_30d is not None and uptime_30d >= 99.9

        return {
            "uptime_1h": uptime_1h,
            "uptime_24h": uptime_24h,
            "uptime_7d": uptime_7d,
            "uptime_30d": uptime_30d,
            "avg_response_ms_24h": self._avg_response_ms(_WINDOW_24H),
            "avg_response_ms_7d": self._avg_response_ms(_WINDOW_7D),
            "checks_24h": checks_24h,
            "checks_7d": checks_7d,
            "checks_30d": checks_30d,
            "total_checks": len(self._checks),
            "last_check": last_check,
            "sla_target": 99.9,
            "sla_met": sla_met,
            "generated_at": now,
        }

    def recent_incidents(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """Return recent unhealthy checks (incidents).

        Args:
            limit: Max incidents to return.

        Returns:
            List of incident dicts, newest first.
        """
        incidents = [
            {
                "timestamp": c.timestamp,
                "response_ms": c.response_ms,
                "details": c.details,
            }
            for c in reversed(self._checks)
            if not c.healthy
        ]
        return incidents[:limit]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_tracker: UptimeTracker | None = None


def get_uptime_tracker() -> UptimeTracker:
    """Return the module-level UptimeTracker singleton."""
    global _tracker  # noqa: PLW0603
    if _tracker is None:
        _tracker = UptimeTracker()
    return _tracker
