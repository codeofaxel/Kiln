"""Background monitor for active fulfillment orders.

Periodically polls external manufacturing providers for order status
updates.  Detects stalled, failed, or cancelled orders and emits
events so the billing system can trigger refunds.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from kiln.events import EventBus
    from kiln.persistence import KilnDB

logger = logging.getLogger(__name__)

# How often to poll (seconds).
_POLL_INTERVAL = 1800  # 30 minutes

# If an order has been in PROCESSING/PRINTING for longer than this,
# flag it as stalled.
_STALL_THRESHOLD = 7 * 24 * 3600  # 7 days


@dataclass
class OrderAlert:
    """An alert about a fulfillment order that needs attention."""

    order_id: str
    alert_type: str  # "stalled", "failed", "cancelled_by_provider"
    message: str
    created_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "alert_type": self.alert_type,
            "message": self.message,
            "created_at": self.created_at,
        }


class FulfillmentMonitor:
    """Background thread that watches active fulfillment orders.

    Polls each active order's status from the manufacturing provider
    and emits events when orders are:
    - Delivered (so we can close the loop)
    - Failed or cancelled by the provider (triggers refund alert)
    - Stalled beyond the expected lead time (triggers alert)
    """

    def __init__(
        self,
        db: KilnDB,
        event_bus: Optional[EventBus] = None,
        *,
        poll_interval: int = _POLL_INTERVAL,
        stall_threshold: int = _STALL_THRESHOLD,
    ) -> None:
        self._db = db
        self._event_bus = event_bus
        self._poll_interval = poll_interval
        self._stall_threshold = stall_threshold
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._alerts: List[OrderAlert] = []
        self._alerts_lock = threading.Lock()

    def start(self) -> None:
        """Start the background monitoring thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="fulfillment-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Fulfillment monitor started (poll every %ds, stall threshold %ds)",
            self._poll_interval,
            self._stall_threshold,
        )

    def stop(self) -> None:
        """Stop the background monitoring thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Fulfillment monitor stopped")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def get_alerts(self) -> List[Dict[str, Any]]:
        """Return current unresolved alerts."""
        with self._alerts_lock:
            return [a.to_dict() for a in self._alerts]

    def _run(self) -> None:
        """Main loop: poll active orders periodically."""
        while not self._stop_event.is_set():
            try:
                self._poll_active_orders()
            except Exception:
                logger.exception("Error in fulfillment monitor poll cycle")
            self._stop_event.wait(self._poll_interval)

    def _poll_active_orders(self) -> None:
        """Check status of all active fulfillment orders."""
        active_orders = self._db.list_active_fulfillment_orders()
        if not active_orders:
            return

        logger.debug("Polling %d active fulfillment orders", len(active_orders))

        for order in active_orders:
            order_id = order.get("order_id", "")
            if not order_id:
                continue

            try:
                self._check_order(order)
            except Exception:
                logger.debug(
                    "Failed to check order %s",
                    order_id,
                    exc_info=True,
                )

    def _check_order(self, order: Dict[str, Any]) -> None:
        """Check a single order and emit events if status changed."""
        order_id = order["order_id"]
        stored_status = order.get("status", "")
        placed_at = order.get("created_at", 0.0)
        now = time.time()

        # Try to get current status from provider.
        try:
            from kiln.fulfillment import get_provider

            provider = get_provider()
            result = provider.get_order_status(order_id)
            current_status = result.status
        except Exception:
            logger.debug("Could not poll status for order %s", order_id)
            return

        # OrderStatus enum → lowercase string for comparison.
        current_str = current_status.value if current_status else ""

        # Detect provider-side failures/cancellations.
        if current_str in ("failed", "cancelled"):
            if stored_status not in ("failed", "cancelled"):
                alert = OrderAlert(
                    order_id=order_id,
                    alert_type="cancelled_by_provider",
                    message=(
                        f"Order {order_id} was {current_str} by the "
                        "manufacturing provider. A refund may be needed."
                    ),
                    created_at=now,
                )
                self._add_alert(alert)
                self._emit("FULFILLMENT_FAILED", {
                    "order_id": order_id,
                    "status": current_str,
                    "message": alert.message,
                })
                # Update local status.
                self._db.update_fulfillment_order_status(
                    order_id, current_str,
                )

        # Detect delivery.
        elif current_str == "delivered":
            if stored_status != "delivered":
                self._emit("FULFILLMENT_DELIVERED", {
                    "order_id": order_id,
                    "status": current_str,
                })
                self._db.update_fulfillment_order_status(
                    order_id, current_str,
                )

        # Detect shipping.
        elif current_str == "shipping":
            if stored_status != "shipping":
                self._emit("FULFILLMENT_SHIPPED", {
                    "order_id": order_id,
                    "status": current_str,
                })
                self._db.update_fulfillment_order_status(
                    order_id, current_str,
                )

        # Detect stalled orders.
        elif current_str in ("processing", "printing"):
            age = now - placed_at
            if age > self._stall_threshold:
                # Only alert once per order.
                existing = any(
                    a.order_id == order_id and a.alert_type == "stalled"
                    for a in self._alerts
                )
                if not existing:
                    days = int(age / 86400)
                    alert = OrderAlert(
                        order_id=order_id,
                        alert_type="stalled",
                        message=(
                            f"Order {order_id} has been in '{current_str}' "
                            f"state for {days} days. Expected lead time may "
                            "have been exceeded."
                        ),
                        created_at=now,
                    )
                    self._add_alert(alert)
                    self._emit("FULFILLMENT_STALLED", {
                        "order_id": order_id,
                        "status": current_str,
                        "days_elapsed": days,
                    })

    def _add_alert(self, alert: OrderAlert) -> None:
        with self._alerts_lock:
            self._alerts.append(alert)
        logger.warning("Fulfillment alert: %s", alert.message)

    def _emit(self, event_name: str, data: Dict[str, Any]) -> None:
        if self._event_bus is None:
            return
        try:
            from kiln.events import EventType

            event_type = EventType[event_name]
            self._event_bus.publish(event_type, data, source="fulfillment_monitor")
        except (KeyError, Exception):
            logger.debug("Could not emit %s event", event_name, exc_info=True)
