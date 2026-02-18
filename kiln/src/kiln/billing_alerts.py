"""Billing alerting -- monitors payment health and emits warnings.

Tracks payment failure patterns and raises alerts when thresholds
are exceeded.  Alerts are logged, emitted as events, and available
via the ``billing_alerts`` MCP tool.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiln.events import Event, EventBus

logger = logging.getLogger(__name__)

# Alert thresholds.
_CONSECUTIVE_FAILURE_THRESHOLD = 3
_FAILURE_RATE_WINDOW = 3600  # 1 hour
_FAILURE_RATE_THRESHOLD = 0.5  # 50% failure rate triggers alert


@dataclass
class BillingAlert:
    """A billing system alert."""

    alert_type: str  # "consecutive_failures", "high_failure_rate", "spend_limit_hit", "provider_error"
    severity: str  # "warning", "critical"
    message: str
    details: dict[str, Any]
    created_at: float
    resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_type": self.alert_type,
            "severity": self.severity,
            "message": self.message,
            "details": self.details,
            "created_at": self.created_at,
            "resolved": self.resolved,
        }


class BillingAlertManager:
    """Monitors payment health and maintains alert state.

    Subscribe to the event bus to receive payment lifecycle events.
    Tracks consecutive failures, failure rates, and spend limit
    violations.

    :param event_bus: Optional event bus to subscribe to.
    """

    def __init__(self, *, event_bus: EventBus | None = None) -> None:
        self._event_bus = event_bus
        self._alerts: list[BillingAlert] = []
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._recent_attempts: list[dict[str, Any]] = []  # (timestamp, success)
        self._subscribed = False

    def subscribe(self) -> None:
        """Wire up event bus listeners."""
        if self._event_bus is None or self._subscribed:
            return
        from kiln.events import EventType

        self._event_bus.subscribe(EventType.PAYMENT_COMPLETED, self._on_payment_success)
        self._event_bus.subscribe(EventType.PAYMENT_FAILED, self._on_payment_failure)
        self._event_bus.subscribe(EventType.SPEND_LIMIT_REACHED, self._on_spend_limit)
        self._subscribed = True
        logger.info("Billing alert manager subscribed to payment events")

    def get_alerts(self, *, include_resolved: bool = False) -> list[dict[str, Any]]:
        """Return current alerts."""
        with self._lock:
            alerts = self._alerts if include_resolved else [a for a in self._alerts if not a.resolved]
            return [a.to_dict() for a in alerts]

    def get_health_summary(self) -> dict[str, Any]:
        """Return a summary of payment system health."""
        now = time.time()
        with self._lock:
            window_start = now - _FAILURE_RATE_WINDOW
            recent = [a for a in self._recent_attempts if a["timestamp"] > window_start]
            total = len(recent)
            failures = sum(1 for a in recent if not a["success"])
            active_alerts = sum(1 for a in self._alerts if not a.resolved)

        return {
            "status": "healthy" if active_alerts == 0 else "degraded",
            "active_alerts": active_alerts,
            "consecutive_failures": self._consecutive_failures,
            "last_hour": {
                "total_attempts": total,
                "failures": failures,
                "failure_rate": round(failures / total, 2) if total > 0 else 0.0,
            },
        }

    # -- Event handlers ---------------------------------------------------

    def _on_payment_success(self, event: Event) -> None:
        """Reset consecutive failure counter on success."""
        now = time.time()
        with self._lock:
            self._consecutive_failures = 0
            self._recent_attempts.append({"timestamp": now, "success": True})
            self._prune_old_attempts(now)

    def _on_payment_failure(self, event: Event) -> None:
        """Track failures and raise alerts if threshold exceeded."""
        now = time.time()
        with self._lock:
            self._consecutive_failures += 1
            self._recent_attempts.append({"timestamp": now, "success": False})
            self._prune_old_attempts(now)

            # Check consecutive failures.
            if self._consecutive_failures >= _CONSECUTIVE_FAILURE_THRESHOLD:
                self._add_alert(
                    BillingAlert(
                        alert_type="consecutive_failures",
                        severity="critical",
                        message=(
                            f"{self._consecutive_failures} consecutive payment failures. "
                            "Check payment provider status and customer payment method."
                        ),
                        details={
                            "count": self._consecutive_failures,
                            "last_error": event.data.get("error", ""),
                            "rail": event.data.get("rail", ""),
                        },
                        created_at=now,
                    )
                )

            # Check failure rate.
            window_start = now - _FAILURE_RATE_WINDOW
            recent = [a for a in self._recent_attempts if a["timestamp"] > window_start]
            if len(recent) >= 5:  # need minimum sample
                rate = sum(1 for a in recent if not a["success"]) / len(recent)
                if rate >= _FAILURE_RATE_THRESHOLD:
                    self._add_alert(
                        BillingAlert(
                            alert_type="high_failure_rate",
                            severity="warning",
                            message=(
                                f"Payment failure rate is {rate:.0%} over the last hour "
                                f"({len(recent)} attempts). Investigate provider health."
                            ),
                            details={"failure_rate": round(rate, 2), "sample_size": len(recent)},
                            created_at=now,
                        )
                    )

    def _on_spend_limit(self, event: Event) -> None:
        """Alert when spend limits are hit."""
        now = time.time()
        with self._lock:
            self._add_alert(
                BillingAlert(
                    alert_type="spend_limit_hit",
                    severity="warning",
                    message=(
                        f"Spend limit reached: {event.data.get('reason', 'unknown')}. "
                        "Review spend limits in billing settings."
                    ),
                    details=event.data,
                    created_at=now,
                )
            )

    # -- Internal helpers -------------------------------------------------

    def _add_alert(self, alert: BillingAlert) -> None:
        """Add alert (lock must be held)."""
        # Deduplicate: don't add same alert_type within 5 minutes.
        cutoff = time.time() - 300
        for existing in self._alerts:
            if existing.alert_type == alert.alert_type and existing.created_at > cutoff and not existing.resolved:
                return
        self._alerts.append(alert)
        logger.warning("BILLING ALERT [%s]: %s", alert.severity.upper(), alert.message)

    def _prune_old_attempts(self, now: float) -> None:
        """Remove attempts older than the tracking window (lock must be held)."""
        cutoff = now - _FAILURE_RATE_WINDOW * 2
        self._recent_attempts = [a for a in self._recent_attempts if a["timestamp"] > cutoff]
