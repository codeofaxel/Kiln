"""Event system — publish/subscribe for printer and job lifecycle events.

Components register callbacks for events they care about.  When something
happens (print started, print failed, temperature warning, etc.) the event
bus dispatches to all registered listeners.

This is used internally to wire the job queue to printer state changes,
and can also be used to drive external webhooks or notifications.

Example::

    bus = EventBus()

    def on_print_done(event: Event) -> None:
        print(f"Print finished on {event.data['printer_name']}")

    bus.subscribe(EventType.PRINT_COMPLETED, on_print_done)
    bus.publish(Event(type=EventType.PRINT_COMPLETED, data={...}))
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class EventType(enum.Enum):
    """All event types emitted by the Kiln system."""

    # Job lifecycle
    JOB_SUBMITTED = "job.submitted"
    JOB_QUEUED = "job.queued"
    JOB_STARTED = "job.started"
    JOB_COMPLETED = "job.completed"
    JOB_FAILED = "job.failed"
    JOB_CANCELLED = "job.cancelled"

    # Printer state
    PRINTER_CONNECTED = "printer.connected"
    PRINTER_DISCONNECTED = "printer.disconnected"
    PRINTER_ERROR = "printer.error"
    PRINTER_IDLE = "printer.idle"

    # Print progress
    PRINT_STARTED = "print.started"
    PRINT_PAUSED = "print.paused"
    PRINT_RESUMED = "print.resumed"
    PRINT_COMPLETED = "print.completed"
    PRINT_FAILED = "print.failed"
    PRINT_CANCELLED = "print.cancelled"
    PRINT_PROGRESS = "print.progress"

    # Safety
    TEMPERATURE_WARNING = "safety.temperature_warning"
    PREFLIGHT_FAILED = "safety.preflight_failed"
    SAFETY_BLOCKED = "safety.blocked"
    SAFETY_ESCALATED = "safety.escalated"
    TOOL_EXECUTED = "safety.tool_executed"

    # File
    FILE_UPLOADED = "file.uploaded"

    # Streaming
    STREAM_STARTED = "stream.started"
    STREAM_STOPPED = "stream.stopped"

    # Cloud sync
    SYNC_COMPLETED = "sync.completed"
    SYNC_FAILED = "sync.failed"

    # Bed leveling
    LEVELING_TRIGGERED = "leveling.triggered"
    LEVELING_COMPLETED = "leveling.completed"
    LEVELING_FAILED = "leveling.failed"
    LEVELING_NEEDED = "leveling.needed"

    # Material tracking
    MATERIAL_LOADED = "material.loaded"
    MATERIAL_MISMATCH = "material.mismatch"
    SPOOL_LOW = "material.spool_low"
    SPOOL_EMPTY = "material.spool_empty"

    # Plugins
    PLUGIN_LOADED = "plugin.loaded"
    PLUGIN_ERROR = "plugin.error"

    # Billing / Payments
    PAYMENT_INITIATED = "payment.initiated"
    PAYMENT_COMPLETED = "payment.completed"
    PAYMENT_FAILED = "payment.failed"
    PAYMENT_REFUNDED = "payment.refunded"
    BILLING_SETUP_COMPLETED = "billing.setup_completed"
    SPEND_LIMIT_REACHED = "billing.spend_limit_reached"

    # Vision monitoring
    VISION_CHECK = "vision.check"
    VISION_ALERT = "vision.alert"


@dataclass
class Event:
    """A single event in the system."""

    type: EventType
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    source: str = ""  # e.g. "printer:voron-350" or "queue"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "type": self.type.value,
            "data": self.data,
            "timestamp": self.timestamp,
            "source": self.source,
        }


# Type alias for event handlers.
EventHandler = Callable[[Event], None]


class EventBus:
    """Thread-safe publish/subscribe event bus.

    Handlers are called synchronously in the publishing thread.  If a
    handler raises, the exception is logged but does not prevent other
    handlers from running.
    """

    def __init__(self) -> None:
        self._handlers: Dict[EventType, List[EventHandler]] = {}
        self._wildcard_handlers: List[EventHandler] = []
        self._lock = threading.Lock()
        self._history: List[Event] = []
        self._max_history: int = 1000

    def subscribe(
        self,
        event_type: Optional[EventType],
        handler: EventHandler,
    ) -> None:
        """Register a handler for a specific event type.

        Args:
            event_type: The event type to listen for, or ``None`` to
                receive ALL events (wildcard subscription).
            handler: Callable that accepts an :class:`Event`.
        """
        with self._lock:
            if event_type is None:
                # Prevent duplicate subscriptions
                for existing in self._wildcard_handlers:
                    if existing is handler:
                        logger.debug("Duplicate subscription for wildcard, skipping")
                        return
                self._wildcard_handlers.append(handler)
            else:
                if event_type not in self._handlers:
                    self._handlers[event_type] = []
                # Prevent duplicate subscriptions
                for existing in self._handlers[event_type]:
                    if existing is handler:
                        logger.debug("Duplicate subscription for %s, skipping", event_type)
                        return
                self._handlers[event_type].append(handler)

    def unsubscribe(
        self,
        event_type: Optional[EventType],
        handler: EventHandler,
    ) -> None:
        """Remove a previously registered handler.

        Silently does nothing if the handler is not found.
        """
        with self._lock:
            if event_type is None:
                try:
                    self._wildcard_handlers.remove(handler)
                except ValueError:
                    pass
            else:
                handlers = self._handlers.get(event_type, [])
                try:
                    handlers.remove(handler)
                except ValueError:
                    pass

    def publish(
        self,
        event_or_type: Event | EventType,
        data: Optional[Dict[str, Any]] = None,
        source: str = "",
    ) -> None:
        """Dispatch an event to all matching handlers.

        Can be called in two ways:
        - ``publish(event)`` -- pass a pre-built :class:`Event`.
        - ``publish(event_type, data_dict, source="...")`` -- build an
          :class:`Event` from the arguments.

        Handlers are invoked synchronously.  Exceptions in handlers are
        logged but do not propagate.
        """
        if isinstance(event_or_type, EventType):
            event = Event(type=event_or_type, data=data or {}, source=source)
        else:
            event = event_or_type
        with self._lock:
            # Record in history
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

            # Collect handlers to call
            specific = list(self._handlers.get(event.type, []))
            wildcards = list(self._wildcard_handlers)

        # Call outside the lock to avoid deadlocks
        for handler in specific + wildcards:
            try:
                handler(event)
            except Exception:
                logger.exception(
                    "Event handler %r failed for %s",
                    handler,
                    event.type.value,
                )

    def recent_events(
        self,
        event_type: Optional[EventType] = None,
        limit: int = 50,
    ) -> List[Event]:
        """Return recent events, newest first.

        Args:
            event_type: Filter by type, or ``None`` for all.
            limit: Maximum number of events to return.
        """
        with self._lock:
            events = list(self._history)

        if event_type is not None:
            events = [e for e in events if e.type == event_type]

        events.reverse()
        return events[:limit]

    def clear_history(self) -> None:
        """Clear the event history buffer."""
        with self._lock:
            self._history.clear()
