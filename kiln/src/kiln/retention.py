"""Data retention policy management for regulatory compliance.

Defines configurable retention windows for each data category (audit logs,
print history, traceability records, agent memory, event logs) and provides
a manager that identifies stale records for purging.

Default retention periods follow manufacturing compliance standards:

- **Traceability records**: 7 years (2555 days) -- ISO 9001 / 21 CFR 820
- **Audit logs**: 1 year (365 days)
- **Print history**: 1 year (365 days)
- **Event logs**: 6 months (180 days)
- **Agent memory**: 90 days

All defaults can be overridden via environment variables or by passing a
custom :class:`RetentionPolicy`.

Usage::

    from kiln.retention import RetentionManager

    mgr = RetentionManager()
    result = mgr.apply(dry_run=True)
    print(result)  # RetentionResult(...)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Policy dataclass
# ---------------------------------------------------------------------------


@dataclass
class RetentionPolicy:
    """Configurable retention windows (in days) per data category.

    :param audit_log_days: How long to keep audit log entries.
    :param print_history_days: How long to keep print history.
    :param traceability_days: How long to keep traceability records
        (default 7 years for manufacturing compliance).
    :param agent_memory_days: How long to keep agent memory entries.
    :param event_log_days: How long to keep event log entries.
    """

    audit_log_days: int = 365
    print_history_days: int = 365
    traceability_days: int = 2555  # ~7 years
    agent_memory_days: int = 90
    event_log_days: int = 180

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "audit_log_days": self.audit_log_days,
            "print_history_days": self.print_history_days,
            "traceability_days": self.traceability_days,
            "agent_memory_days": self.agent_memory_days,
            "event_log_days": self.event_log_days,
        }


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class RetentionResult:
    """Outcome of a retention policy application.

    :param dry_run: Whether the operation was a dry run.
    :param audit_log_expired: Number of audit log records past retention.
    :param print_history_expired: Number of print history records past retention.
    :param traceability_expired: Number of traceability records past retention.
    :param agent_memory_expired: Number of agent memory records past retention.
    :param event_log_expired: Number of event log records past retention.
    """

    dry_run: bool
    audit_log_expired: int = 0
    print_history_expired: int = 0
    traceability_expired: int = 0
    agent_memory_expired: int = 0
    event_log_expired: int = 0

    @property
    def total_expired(self) -> int:
        """Total number of records identified as expired."""
        return (
            self.audit_log_expired
            + self.print_history_expired
            + self.traceability_expired
            + self.agent_memory_expired
            + self.event_log_expired
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "dry_run": self.dry_run,
            "audit_log_expired": self.audit_log_expired,
            "print_history_expired": self.print_history_expired,
            "traceability_expired": self.traceability_expired,
            "agent_memory_expired": self.agent_memory_expired,
            "event_log_expired": self.event_log_expired,
            "total_expired": self.total_expired,
        }


# ---------------------------------------------------------------------------
# Environment variable helpers
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    """Read an integer from an environment variable, falling back to *default*."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
        if value < 1:
            logger.warning(
                "Env var %s=%s is < 1; using minimum of 1 day",
                name,
                raw,
            )
            return 1
        return value
    except ValueError:
        logger.warning(
            "Env var %s=%s is not a valid integer; using default %d",
            name,
            raw,
            default,
        )
        return default


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class RetentionManager:
    """Applies data retention policies to identify and purge stale records.

    Environment variable overrides (checked at construction time):

    - ``KILN_RETENTION_AUDIT_DAYS``
    - ``KILN_RETENTION_PRINT_DAYS``
    - ``KILN_RETENTION_TRACE_DAYS``
    """

    def __init__(self, *, policy: RetentionPolicy | None = None) -> None:
        if policy is not None:
            self._policy = policy
        else:
            self._policy = RetentionPolicy(
                audit_log_days=_env_int(
                    "KILN_RETENTION_AUDIT_DAYS",
                    RetentionPolicy.audit_log_days,
                ),
                print_history_days=_env_int(
                    "KILN_RETENTION_PRINT_DAYS",
                    RetentionPolicy.print_history_days,
                ),
                traceability_days=_env_int(
                    "KILN_RETENTION_TRACE_DAYS",
                    RetentionPolicy.traceability_days,
                ),
            )

    def get_policy(self) -> RetentionPolicy:
        """Return the currently active retention policy."""
        return self._policy

    def apply(self, *, dry_run: bool = True) -> RetentionResult:
        """Identify records older than the policy thresholds.

        In ``dry_run`` mode (the default), nothing is deleted -- the result
        simply reports what *would* be removed.  When ``dry_run=False``,
        records are purged from the backing store.

        .. note::
            The current implementation has no backing store and always returns
            zero counts.  When a storage backend is wired in, this method
            will query each table for records older than the cutoff.

        :returns: A :class:`RetentionResult` with per-category counts.
        """
        now = datetime.now(timezone.utc)

        cutoffs = {
            "audit_log": now - timedelta(days=self._policy.audit_log_days),
            "print_history": now - timedelta(days=self._policy.print_history_days),
            "traceability": now - timedelta(days=self._policy.traceability_days),
            "agent_memory": now - timedelta(days=self._policy.agent_memory_days),
            "event_log": now - timedelta(days=self._policy.event_log_days),
        }

        logger.info(
            "Retention %s: cutoffs=%s",
            "dry_run" if dry_run else "apply",
            {k: v.isoformat() for k, v in cutoffs.items()},
        )

        # No backing store wired yet -- return empty result with cutoff info.
        return RetentionResult(dry_run=dry_run)
