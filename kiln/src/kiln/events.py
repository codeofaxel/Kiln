"""Event system — publish/subscribe for printer and job lifecycle events.

Components register callbacks for events they care about.  When something
happens (print started, print failed, temperature warning, etc.) the event
bus dispatches to all registered listeners.

This is used internally to wire the job queue to printer state changes,
and can also be used to drive external webhooks or notifications.

Provides both a synchronous :class:`EventBus` for thread-based callers
and an :class:`AsyncEventBus` backed by ``asyncio.Queue`` for
non-blocking dispatch in async hot paths.

Example (sync)::

    bus = EventBus()

    def on_print_done(event: Event) -> None:
        print(f"Print finished on {event.data['printer_name']}")

    bus.subscribe(EventType.PRINT_COMPLETED, on_print_done)
    bus.publish(Event(type=EventType.PRINT_COMPLETED, data={...}))

Example (async)::

    async_bus = AsyncEventBus()
    await async_bus.start()

    async def on_print_done(event: Event) -> None:
        print(f"Print finished on {event.data['printer_name']}")

    await async_bus.subscribe(EventType.PRINT_COMPLETED, on_print_done)
    await async_bus.publish(Event(type=EventType.PRINT_COMPLETED, data={...}))
"""

from __future__ import annotations

import asyncio
import enum
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from kiln import parse_int_env

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
    JOB_STUCK_TIMEOUT = "job.stuck_timeout"

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
    PRINT_TERMINAL = "print.terminal"

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
    PAYMENT_PROCESSING = "payment.processing"
    PAYMENT_REFUNDED = "payment.refunded"
    BILLING_SETUP_COMPLETED = "billing.setup_completed"
    SPEND_LIMIT_REACHED = "billing.spend_limit_reached"

    # Fulfillment / outsourced manufacturing
    FULFILLMENT_QUOTED = "fulfillment.quoted"
    FULFILLMENT_ORDERED = "fulfillment.ordered"
    FULFILLMENT_PROCESSING = "fulfillment.processing"
    FULFILLMENT_PRINTING = "fulfillment.printing"
    FULFILLMENT_SHIPPING = "fulfillment.shipping"
    FULFILLMENT_SHIPPED = "fulfillment.shipped"
    FULFILLMENT_DELIVERED = "fulfillment.delivered"
    FULFILLMENT_CANCELLED = "fulfillment.cancelled"
    FULFILLMENT_FAILED = "fulfillment.failed"
    FULFILLMENT_STALLED = "fulfillment.stalled"

    # Vision monitoring
    VISION_CHECK = "vision.check"
    VISION_ALERT = "vision.alert"


@dataclass
class Event:
    """A single event in the system."""

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    source: str = ""  # e.g. "printer:voron-350" or "queue"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "type": self.type.value,
            "data": self.data,
            "timestamp": self.timestamp,
            "source": self.source,
        }


# Type aliases for event handlers.
EventHandler = Callable[[Event], None]
AsyncEventHandler = Callable[[Event], Awaitable[None]]
EventFilter = Callable[[Event], bool]

# Default queue size for AsyncEventBus, configurable via KILN_EVENT_QUEUE_SIZE.
_DEFAULT_QUEUE_SIZE = 10_000


class EventBus:
    """Thread-safe publish/subscribe event bus.

    Handlers are called synchronously in the publishing thread.  If a
    handler raises, the exception is logged but does not prevent other
    handlers from running.

    Subscribers may provide an optional *filter* predicate that is
    evaluated before the handler is called.
    """

    def __init__(self) -> None:
        self._handlers: dict[EventType, list[tuple[EventHandler, EventFilter | None]]] = {}
        self._wildcard_handlers: list[tuple[EventHandler, EventFilter | None]] = []
        self._lock = threading.Lock()
        self._history: list[Event] = []
        self._max_history: int = 1000

    def subscribe(
        self,
        event_type: EventType | None,
        handler: EventHandler,
        *,
        filter: EventFilter | None = None,
    ) -> None:
        """Register a handler for a specific event type.

        :param event_type: The event type to listen for, or ``None`` to
            receive ALL events (wildcard subscription).
        :param handler: Callable that accepts an :class:`Event`.
        :param filter: Optional predicate ``(Event) -> bool``.  When
            provided, the handler is only called if the predicate
            returns ``True`` for the published event.
        """
        with self._lock:
            if event_type is None:
                for existing, _ in self._wildcard_handlers:
                    if existing is handler:
                        logger.debug("Duplicate subscription for wildcard, skipping")
                        return
                self._wildcard_handlers.append((handler, filter))
            else:
                if event_type not in self._handlers:
                    self._handlers[event_type] = []
                for existing, _ in self._handlers[event_type]:
                    if existing is handler:
                        logger.debug("Duplicate subscription for %s, skipping", event_type)
                        return
                self._handlers[event_type].append((handler, filter))

    def unsubscribe(
        self,
        event_type: EventType | None,
        handler: EventHandler,
    ) -> None:
        """Remove a previously registered handler.

        Silently does nothing if the handler is not found.
        """
        with self._lock:
            if event_type is None:
                self._wildcard_handlers = [(h, f) for h, f in self._wildcard_handlers if h is not handler]
            else:
                entries = self._handlers.get(event_type, [])
                self._handlers[event_type] = [(h, f) for h, f in entries if h is not handler]

    # ------------------------------------------------------------------
    # Internal dispatch helper
    # ------------------------------------------------------------------

    def _dispatch_to_handlers(
        self,
        event: Event,
        handlers: list[tuple[EventHandler, EventFilter | None]],
    ) -> None:
        """Call each handler whose filter (if any) passes for *event*."""
        for handler, filt in handlers:
            try:
                if filt is not None and not filt(event):
                    continue
                handler(event)
            except Exception:
                logger.exception(
                    "Event handler %r failed for %s",
                    handler,
                    event.type.value,
                )

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    def _resolve_event(
        self,
        event_or_type: Event | EventType,
        data: dict[str, Any] | None,
        source: str,
    ) -> Event:
        """Normalise the flexible publish signature into an :class:`Event`."""
        if isinstance(event_or_type, EventType):
            return Event(type=event_or_type, data=data or {}, source=source)
        return event_or_type

    def publish(
        self,
        event_or_type: Event | EventType,
        data: dict[str, Any] | None = None,
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
        event = self._resolve_event(event_or_type, data, source)
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history :]
            specific = list(self._handlers.get(event.type, []))
            wildcards = list(self._wildcard_handlers)

        # Call outside the lock to avoid deadlocks.
        self._dispatch_to_handlers(event, specific + wildcards)

    def publish_batch(self, events: list[Event]) -> None:
        """Publish multiple events atomically.

        All events are recorded in history under a single lock
        acquisition, then handlers are dispatched sequentially.
        Useful for saga patterns where multiple state changes happen
        together.
        """
        if not events:
            return

        all_targets: list[tuple[Event, list[tuple[EventHandler, EventFilter | None]]]] = []
        with self._lock:
            for event in events:
                self._history.append(event)
                specific = list(self._handlers.get(event.type, []))
                wildcards = list(self._wildcard_handlers)
                all_targets.append((event, specific + wildcards))
            # Trim history once after recording all events.
            overflow = len(self._history) - self._max_history
            if overflow > 0:
                self._history = self._history[overflow:]

        for event, handlers in all_targets:
            self._dispatch_to_handlers(event, handlers)

    def dispatch_async(
        self,
        event_or_type: Event | EventType,
        data: dict[str, Any] | None = None,
        source: str = "",
    ) -> None:
        """Schedule an event for async delivery if an event loop is running.

        Uses ``asyncio.run_coroutine_threadsafe`` to enqueue the event
        onto a running :class:`AsyncEventBus` consumer.  Falls back to
        synchronous :meth:`publish` when no event loop is available.

        This method is safe to call from any thread.
        """
        event = self._resolve_event(event_or_type, data, source)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            # Schedule onto the running loop.  The coroutine records
            # history and dispatches asynchronously, but we still
            # record in *this* bus synchronously so callers that
            # query recent_events() immediately see the event.
            with self._lock:
                self._history.append(event)
                if len(self._history) > self._max_history:
                    self._history = self._history[-self._max_history :]
                specific = list(self._handlers.get(event.type, []))
                wildcards = list(self._wildcard_handlers)

            async def _dispatch() -> None:
                self._dispatch_to_handlers(event, specific + wildcards)

            asyncio.run_coroutine_threadsafe(_dispatch(), loop)
        else:
            # No event loop — fall back to synchronous dispatch.
            self.publish(event)

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def recent_events(
        self,
        event_type: EventType | None = None,
        limit: int = 50,
        event_type_prefix: str | None = None,
    ) -> list[Event]:
        """Return recent events, newest first.

        :param event_type: Filter by exact type, or ``None`` for all.
        :param limit: Maximum number of events to return.
        :param event_type_prefix: Filter by event type value prefix
            (e.g. ``"print"`` matches ``print.started``,
            ``print.completed``, etc.).  Ignored if *event_type*
            is also provided (exact match takes precedence).
        """
        with self._lock:
            events = list(self._history)

        if event_type is not None:
            events = [e for e in events if e.type == event_type]
        elif event_type_prefix is not None:
            prefix = event_type_prefix if "." in event_type_prefix else event_type_prefix + "."
            events = [e for e in events if e.type.value.startswith(prefix)]

        events.reverse()
        return events[:limit]

    def clear_history(self) -> None:
        """Clear the event history buffer."""
        with self._lock:
            self._history.clear()


# ---------------------------------------------------------------------------
# AsyncEventBus — non-blocking, asyncio-native event dispatch
# ---------------------------------------------------------------------------


class AsyncEventBus:
    """Async event bus backed by :class:`asyncio.Queue`.

    Events are published into a bounded queue and consumed by a
    background task that dispatches to registered async callbacks.
    The queue size defaults to ``10,000`` and is configurable via
    the ``KILN_EVENT_QUEUE_SIZE`` environment variable.

    Lifecycle::

        bus = AsyncEventBus()
        await bus.start()     # spawns the consumer task
        ...
        await bus.stop()      # drains remaining events, then exits
    """

    def __init__(self, *, queue_size: int | None = None) -> None:
        size = (
            queue_size
            if queue_size is not None
            else parse_int_env("KILN_EVENT_QUEUE_SIZE", _DEFAULT_QUEUE_SIZE)
        )
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=size)
        self._handlers: dict[EventType, list[tuple[AsyncEventHandler, EventFilter | None]]] = {}
        self._wildcard_handlers: list[tuple[AsyncEventHandler, EventFilter | None]] = []
        self._lock = asyncio.Lock()
        self._consumer_task: asyncio.Task[None] | None = None
        self._history: list[Event] = []
        self._max_history: int = 1000

    async def start(self) -> None:
        """Start the background consumer task."""
        if self._consumer_task is not None and not self._consumer_task.done():
            return
        self._consumer_task = asyncio.ensure_future(self._consumer())
        logger.debug("AsyncEventBus consumer started")

    async def stop(self) -> None:
        """Signal the consumer to stop and wait for it to drain."""
        if self._consumer_task is None or self._consumer_task.done():
            return
        # Sentinel value signals the consumer to exit.
        await self._queue.put(None)
        await self._consumer_task
        self._consumer_task = None
        logger.debug("AsyncEventBus consumer stopped")

    @property
    def running(self) -> bool:
        """Return ``True`` if the consumer task is active."""
        return self._consumer_task is not None and not self._consumer_task.done()

    @property
    def queue_size(self) -> int:
        """Current number of events waiting in the queue."""
        return self._queue.qsize()

    # ------------------------------------------------------------------
    # Subscribe / unsubscribe
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        event_type: EventType | None,
        handler: AsyncEventHandler,
        *,
        filter: EventFilter | None = None,
    ) -> None:
        """Register an async handler for a specific event type.

        :param event_type: The event type to listen for, or ``None`` for
            wildcard (all events).
        :param handler: Async callable that accepts an :class:`Event`.
        :param filter: Optional predicate ``(Event) -> bool``.
        """
        async with self._lock:
            if event_type is None:
                for existing, _ in self._wildcard_handlers:
                    if existing is handler:
                        return
                self._wildcard_handlers.append((handler, filter))
            else:
                if event_type not in self._handlers:
                    self._handlers[event_type] = []
                for existing, _ in self._handlers[event_type]:
                    if existing is handler:
                        return
                self._handlers[event_type].append((handler, filter))

    async def unsubscribe(
        self,
        event_type: EventType | None,
        handler: AsyncEventHandler,
    ) -> None:
        """Remove a previously registered async handler."""
        async with self._lock:
            if event_type is None:
                self._wildcard_handlers = [(h, f) for h, f in self._wildcard_handlers if h is not handler]
            else:
                entries = self._handlers.get(event_type, [])
                self._handlers[event_type] = [(h, f) for h, f in entries if h is not handler]

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(
        self,
        event_or_type: Event | EventType,
        data: dict[str, Any] | None = None,
        source: str = "",
    ) -> None:
        """Enqueue an event for async dispatch.

        Non-blocking as long as the queue is not full.  Raises
        :class:`asyncio.QueueFull` if the bounded queue is at capacity.
        """
        if isinstance(event_or_type, EventType):
            event = Event(type=event_or_type, data=data or {}, source=source)
        else:
            event = event_or_type
        self._queue.put_nowait(event)

    async def publish_batch(self, events: list[Event]) -> None:
        """Enqueue multiple events atomically.

        All events are placed into the queue in order.  If any single
        put would exceed the queue capacity, :class:`asyncio.QueueFull`
        is raised and **none** of the events from that point onward are
        enqueued (events already enqueued before the overflow remain).
        """
        for event in events:
            self._queue.put_nowait(event)

    # ------------------------------------------------------------------
    # Consumer
    # ------------------------------------------------------------------

    async def _consumer(self) -> None:
        """Background task: drain queue and dispatch to subscribers."""
        while True:
            event = await self._queue.get()
            if event is None:
                # Sentinel — shut down.
                self._queue.task_done()
                break
            try:
                await self._dispatch(event)
            except Exception:
                logger.exception(
                    "Unhandled error in async event consumer for %s",
                    event.type.value,
                )
            finally:
                self._queue.task_done()

    async def _dispatch(self, event: Event) -> None:
        """Dispatch a single event to matching handlers."""
        # Record in history.
        async with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history :]
            specific = list(self._handlers.get(event.type, []))
            wildcards = list(self._wildcard_handlers)

        for handler, filt in specific + wildcards:
            try:
                if filt is not None and not filt(event):
                    continue
                await handler(event)
            except Exception:
                logger.exception(
                    "Async event handler %r failed for %s",
                    handler,
                    event.type.value,
                )

    # ------------------------------------------------------------------
    # History (mirrors synchronous EventBus API)
    # ------------------------------------------------------------------

    async def recent_events(
        self,
        event_type: EventType | None = None,
        limit: int = 50,
        event_type_prefix: str | None = None,
    ) -> list[Event]:
        """Return recent events, newest first."""
        async with self._lock:
            events = list(self._history)

        if event_type is not None:
            events = [e for e in events if e.type == event_type]
        elif event_type_prefix is not None:
            prefix = event_type_prefix if "." in event_type_prefix else event_type_prefix + "."
            events = [e for e in events if e.type.value.startswith(prefix)]

        events.reverse()
        return events[:limit]

    async def clear_history(self) -> None:
        """Clear the event history buffer."""
        async with self._lock:
            self._history.clear()
