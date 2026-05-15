"""Tests for kiln.events -- publish/subscribe event bus.

Covers:
- Subscribe/unsubscribe handlers
- Publish events, handlers called
- Wildcard subscriptions
- Recent events history
- Clear history
- Event.to_dict
- EventType enum values
- Thread safety
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from kiln.events import Event, EventBus, EventType

# ---------------------------------------------------------------------------
# EventType enum
# ---------------------------------------------------------------------------

class TestEventType:
    """Tests for the EventType enum."""

    def test_job_lifecycle_events(self):
        assert EventType.JOB_QUEUED.value == "job.queued"
        assert EventType.JOB_STARTED.value == "job.started"
        assert EventType.JOB_COMPLETED.value == "job.completed"
        assert EventType.JOB_FAILED.value == "job.failed"
        assert EventType.JOB_CANCELLED.value == "job.cancelled"

    def test_printer_state_events(self):
        assert EventType.PRINTER_CONNECTED.value == "printer.connected"
        assert EventType.PRINTER_DISCONNECTED.value == "printer.disconnected"
        assert EventType.PRINTER_ERROR.value == "printer.error"
        assert EventType.PRINTER_IDLE.value == "printer.idle"

    def test_print_progress_events(self):
        assert EventType.PRINT_STARTED.value == "print.started"
        assert EventType.PRINT_PAUSED.value == "print.paused"
        assert EventType.PRINT_RESUMED.value == "print.resumed"
        assert EventType.PRINT_COMPLETED.value == "print.completed"
        assert EventType.PRINT_FAILED.value == "print.failed"
        assert EventType.PRINT_CANCELLED.value == "print.cancelled"
        assert EventType.PRINT_PROGRESS.value == "print.progress"

    def test_safety_events(self):
        assert EventType.TEMPERATURE_WARNING.value == "safety.temperature_warning"
        assert EventType.PREFLIGHT_FAILED.value == "safety.preflight_failed"

    def test_file_events(self):
        assert EventType.FILE_UPLOADED.value == "file.uploaded"

    def test_all_members_count(self):
        assert len(EventType) == 62

    def test_from_value(self):
        assert EventType("job.queued") is EventType.JOB_QUEUED
        assert EventType("print.completed") is EventType.PRINT_COMPLETED

    def test_invalid_value(self):
        with pytest.raises(ValueError):
            EventType("nonexistent.event")


# ---------------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------------

class TestEvent:
    """Tests for the Event dataclass."""

    def test_to_dict_converts_enum(self):
        event = Event(
            type=EventType.JOB_QUEUED,
            data={"job_id": "abc"},
            timestamp=1000.0,
            source="queue",
        )
        d = event.to_dict()
        assert d["type"] == "job.queued"
        assert d["data"] == {"job_id": "abc"}
        assert d["timestamp"] == 1000.0
        assert d["source"] == "queue"

    def test_to_dict_all_keys(self):
        event = Event(type=EventType.PRINT_STARTED)
        d = event.to_dict()
        assert set(d.keys()) == {"type", "data", "timestamp", "source"}

    def test_default_values(self):
        event = Event(type=EventType.PRINTER_IDLE)
        assert event.data == {}
        assert event.source == ""
        assert isinstance(event.timestamp, float)

    def test_custom_data(self):
        data = {"printer_name": "voron", "temperature": 205.0}
        event = Event(type=EventType.TEMPERATURE_WARNING, data=data)
        assert event.data == data

    def test_to_dict_does_not_mutate_original(self):
        event = Event(type=EventType.FILE_UPLOADED, data={"file": "test.gcode"})
        d = event.to_dict()
        d["type"] = "hacked"
        d["data"]["extra"] = "injected"
        assert event.type == EventType.FILE_UPLOADED
        # Note: data dict is shared (shallow), this is expected behaviour.


# ---------------------------------------------------------------------------
# EventBus subscribe / unsubscribe
# ---------------------------------------------------------------------------

class TestEventBusSubscription:
    """Tests for subscribe and unsubscribe."""

    def test_subscribe_specific_event(self):
        bus = EventBus()
        handler = MagicMock()
        bus.subscribe(EventType.JOB_QUEUED, handler)
        bus.publish(Event(type=EventType.JOB_QUEUED))
        handler.assert_called_once()

    def test_subscribe_does_not_fire_for_other_events(self):
        bus = EventBus()
        handler = MagicMock()
        bus.subscribe(EventType.JOB_QUEUED, handler)
        bus.publish(Event(type=EventType.JOB_COMPLETED))
        handler.assert_not_called()

    def test_multiple_handlers_for_same_event(self):
        bus = EventBus()
        handler1 = MagicMock()
        handler2 = MagicMock()
        bus.subscribe(EventType.PRINT_STARTED, handler1)
        bus.subscribe(EventType.PRINT_STARTED, handler2)
        bus.publish(Event(type=EventType.PRINT_STARTED))
        handler1.assert_called_once()
        handler2.assert_called_once()

    def test_unsubscribe_specific_handler(self):
        bus = EventBus()
        handler = MagicMock()
        bus.subscribe(EventType.JOB_QUEUED, handler)
        bus.unsubscribe(EventType.JOB_QUEUED, handler)
        bus.publish(Event(type=EventType.JOB_QUEUED))
        handler.assert_not_called()

    def test_unsubscribe_nonexistent_handler_is_silent(self):
        bus = EventBus()
        handler = MagicMock()
        # Should not raise
        bus.unsubscribe(EventType.JOB_QUEUED, handler)

    def test_unsubscribe_only_removes_specified_handler(self):
        bus = EventBus()
        handler1 = MagicMock()
        handler2 = MagicMock()
        bus.subscribe(EventType.JOB_QUEUED, handler1)
        bus.subscribe(EventType.JOB_QUEUED, handler2)
        bus.unsubscribe(EventType.JOB_QUEUED, handler1)
        bus.publish(Event(type=EventType.JOB_QUEUED))
        handler1.assert_not_called()
        handler2.assert_called_once()


# ---------------------------------------------------------------------------
# EventBus publish
# ---------------------------------------------------------------------------

class TestEventBusPublish:
    """Tests for publish."""

    def test_handler_receives_event_object(self):
        bus = EventBus()
        received: list[Event] = []
        bus.subscribe(EventType.PRINT_COMPLETED, received.append)

        event = Event(
            type=EventType.PRINT_COMPLETED,
            data={"printer": "voron"},
            source="test",
        )
        bus.publish(event)

        assert len(received) == 1
        assert received[0] is event

    def test_handler_exception_does_not_prevent_others(self):
        bus = EventBus()
        handler1 = MagicMock(side_effect=ValueError("handler1 broke"))
        handler2 = MagicMock()
        bus.subscribe(EventType.JOB_FAILED, handler1)
        bus.subscribe(EventType.JOB_FAILED, handler2)

        bus.publish(Event(type=EventType.JOB_FAILED, data={"error": "thermal"}))

        handler1.assert_called_once()
        handler2.assert_called_once()

    def test_publish_to_empty_bus(self):
        bus = EventBus()
        # Should not raise
        bus.publish(Event(type=EventType.PRINT_PROGRESS))

    def test_multiple_publishes(self):
        bus = EventBus()
        handler = MagicMock()
        bus.subscribe(EventType.PRINT_PROGRESS, handler)

        for _ in range(5):
            bus.publish(Event(type=EventType.PRINT_PROGRESS))

        assert handler.call_count == 5


# ---------------------------------------------------------------------------
# Wildcard subscriptions
# ---------------------------------------------------------------------------

class TestEventBusWildcard:
    """Tests for wildcard (event_type=None) subscriptions."""

    def test_wildcard_receives_all_events(self):
        bus = EventBus()
        received: list[Event] = []
        bus.subscribe(None, received.append)

        bus.publish(Event(type=EventType.JOB_QUEUED))
        bus.publish(Event(type=EventType.PRINT_STARTED))
        bus.publish(Event(type=EventType.TEMPERATURE_WARNING))

        assert len(received) == 3
        assert received[0].type == EventType.JOB_QUEUED
        assert received[1].type == EventType.PRINT_STARTED
        assert received[2].type == EventType.TEMPERATURE_WARNING

    def test_wildcard_and_specific_both_called(self):
        bus = EventBus()
        wildcard_handler = MagicMock()
        specific_handler = MagicMock()
        bus.subscribe(None, wildcard_handler)
        bus.subscribe(EventType.JOB_QUEUED, specific_handler)

        bus.publish(Event(type=EventType.JOB_QUEUED))

        wildcard_handler.assert_called_once()
        specific_handler.assert_called_once()

    def test_unsubscribe_wildcard(self):
        bus = EventBus()
        handler = MagicMock()
        bus.subscribe(None, handler)
        bus.unsubscribe(None, handler)

        bus.publish(Event(type=EventType.JOB_QUEUED))
        handler.assert_not_called()

    def test_unsubscribe_wildcard_nonexistent_is_silent(self):
        bus = EventBus()
        handler = MagicMock()
        # Should not raise
        bus.unsubscribe(None, handler)

    def test_wildcard_exception_does_not_block_others(self):
        bus = EventBus()
        bad_handler = MagicMock(side_effect=RuntimeError("boom"))
        good_handler = MagicMock()
        bus.subscribe(None, bad_handler)
        bus.subscribe(None, good_handler)

        bus.publish(Event(type=EventType.PRINTER_ERROR))

        bad_handler.assert_called_once()
        good_handler.assert_called_once()


# ---------------------------------------------------------------------------
# Recent events history
# ---------------------------------------------------------------------------

class TestEventBusHistory:
    """Tests for recent_events and history management."""

    def test_recent_events_records_published(self):
        bus = EventBus()
        bus.publish(Event(type=EventType.JOB_QUEUED))
        bus.publish(Event(type=EventType.JOB_STARTED))

        events = bus.recent_events()
        assert len(events) == 2

    def test_recent_events_newest_first(self):
        bus = EventBus()
        bus.publish(Event(type=EventType.JOB_QUEUED, timestamp=100.0))
        bus.publish(Event(type=EventType.JOB_STARTED, timestamp=200.0))

        events = bus.recent_events()
        assert events[0].type == EventType.JOB_STARTED
        assert events[1].type == EventType.JOB_QUEUED

    def test_recent_events_filter_by_type(self):
        bus = EventBus()
        bus.publish(Event(type=EventType.JOB_QUEUED))
        bus.publish(Event(type=EventType.PRINT_STARTED))
        bus.publish(Event(type=EventType.JOB_QUEUED))

        events = bus.recent_events(event_type=EventType.JOB_QUEUED)
        assert len(events) == 2
        assert all(e.type == EventType.JOB_QUEUED for e in events)

    def test_recent_events_limit(self):
        bus = EventBus()
        for _ in range(10):
            bus.publish(Event(type=EventType.PRINT_PROGRESS))

        events = bus.recent_events(limit=3)
        assert len(events) == 3

    def test_recent_events_empty_bus(self):
        bus = EventBus()
        assert bus.recent_events() == []

    def test_history_max_size(self):
        bus = EventBus()
        bus._max_history = 5

        for i in range(10):
            bus.publish(Event(type=EventType.PRINT_PROGRESS, data={"i": i}))

        events = bus.recent_events()
        assert len(events) == 5
        # Newest first: the last published (i=9) should be first
        assert events[0].data["i"] == 9

    def test_recent_events_filter_and_limit(self):
        bus = EventBus()
        for _ in range(5):
            bus.publish(Event(type=EventType.JOB_QUEUED))
        for _ in range(5):
            bus.publish(Event(type=EventType.PRINT_PROGRESS))

        events = bus.recent_events(event_type=EventType.JOB_QUEUED, limit=2)
        assert len(events) == 2
        assert all(e.type == EventType.JOB_QUEUED for e in events)


# ---------------------------------------------------------------------------
# Clear history
# ---------------------------------------------------------------------------

class TestEventBusClearHistory:
    """Tests for clear_history."""

    def test_clear_removes_all_events(self):
        bus = EventBus()
        bus.publish(Event(type=EventType.JOB_QUEUED))
        bus.publish(Event(type=EventType.JOB_STARTED))
        assert len(bus.recent_events()) == 2

        bus.clear_history()
        assert bus.recent_events() == []

    def test_clear_empty_history(self):
        bus = EventBus()
        # Should not raise
        bus.clear_history()
        assert bus.recent_events() == []

    def test_clear_does_not_remove_subscriptions(self):
        bus = EventBus()
        handler = MagicMock()
        bus.subscribe(EventType.JOB_QUEUED, handler)

        bus.publish(Event(type=EventType.JOB_QUEUED))
        bus.clear_history()

        bus.publish(Event(type=EventType.JOB_QUEUED))
        assert handler.call_count == 2
        # Only the post-clear event should be in history
        history = bus.recent_events()
        assert len(history) == 1


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestEventBusThreadSafety:
    """Tests for thread-safe concurrent operations."""

    def test_concurrent_publish(self):
        bus = EventBus()
        received: list[Event] = []
        lock = threading.Lock()

        def thread_safe_handler(event: Event) -> None:
            with lock:
                received.append(event)

        bus.subscribe(None, thread_safe_handler)

        def publish_batch(count: int) -> None:
            for _ in range(count):
                bus.publish(Event(type=EventType.PRINT_PROGRESS))

        threads = [threading.Thread(target=publish_batch, args=(20,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(received) == 100

    def test_concurrent_subscribe_and_publish(self):
        bus = EventBus()
        errors: list[Exception] = []

        def subscribe_batch() -> None:
            for _ in range(20):
                bus.subscribe(EventType.JOB_QUEUED, lambda e: None)

        def publish_batch() -> None:
            for _ in range(20):
                try:
                    bus.publish(Event(type=EventType.JOB_QUEUED))
                except Exception as exc:
                    errors.append(exc)

        t1 = threading.Thread(target=subscribe_batch)
        t2 = threading.Thread(target=publish_batch)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(errors) == 0

    def test_concurrent_publish_history_integrity(self):
        bus = EventBus()

        def publish_batch(event_type: EventType, count: int) -> None:
            for _ in range(count):
                bus.publish(Event(type=event_type))

        t1 = threading.Thread(target=publish_batch, args=(EventType.JOB_QUEUED, 50))
        t2 = threading.Thread(target=publish_batch, args=(EventType.PRINT_STARTED, 50))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        events = bus.recent_events(limit=200)
        assert len(events) == 100

        queued = [e for e in events if e.type == EventType.JOB_QUEUED]
        started = [e for e in events if e.type == EventType.PRINT_STARTED]
        assert len(queued) == 50
        assert len(started) == 50


# ---------------------------------------------------------------------------
# Ambient actor context
# ---------------------------------------------------------------------------


class TestActorContext:
    """Tests for ``current_actor_context`` and its propagation onto events.

    The ContextVar is a generic "who triggered this event" envelope.  An
    orchestrator (e.g. kiln-pro's REST dispatcher) sets it before invoking
    code that publishes events; :class:`EventBus` attaches its current
    value to every event whose ``actor`` is not already set.  This test
    class pins the contract — bus-level behaviour only; cross-repo
    integration is tested kiln-pro-side.
    """

    def test_actor_defaults_to_none_on_event(self):
        """The dataclass default — un-stamped Events have actor=None."""
        e = Event(type=EventType.JOB_QUEUED, data={"x": 1})
        assert e.actor is None

    def test_context_var_default_is_none(self):
        from kiln.events import current_actor_context

        assert current_actor_context.get() is None

    def test_publish_without_context_leaves_actor_none(self):
        from kiln.events import current_actor_context

        bus = EventBus()
        # ContextVar default is None — sanity that no test before us leaked.
        assert current_actor_context.get() is None
        bus.publish(EventType.JOB_QUEUED, {"job_id": "j1"})
        events = bus.recent_events()
        assert len(events) == 1
        assert events[0].actor is None

    def test_publish_with_ambient_context_stamps_event(self):
        from kiln.events import current_actor_context

        bus = EventBus()
        token = current_actor_context.set({"caller_tier": "pro", "caller_id": "t-7"})
        try:
            bus.publish(EventType.JOB_QUEUED, {"job_id": "j2"})
        finally:
            current_actor_context.reset(token)

        events = bus.recent_events()
        assert len(events) == 1
        assert events[0].actor == {"caller_tier": "pro", "caller_id": "t-7"}

    def test_explicit_event_actor_wins_over_ambient(self):
        """Pre-built Event with actor set is NOT overwritten by the
        ambient ContextVar — explicit always beats ambient."""
        from kiln.events import current_actor_context

        bus = EventBus()
        explicit = Event(
            type=EventType.JOB_QUEUED,
            data={"job_id": "j3"},
            actor={"caller_tier": "business"},
        )
        token = current_actor_context.set({"caller_tier": "free"})
        try:
            bus.publish(explicit)
        finally:
            current_actor_context.reset(token)

        events = bus.recent_events()
        assert len(events) == 1
        assert events[0].actor == {"caller_tier": "business"}

    def test_actor_snapshot_is_defensive_copy(self):
        """Mutating the ambient dict AFTER publish must not mutate the
        already-stamped event's actor."""
        from kiln.events import current_actor_context

        bus = EventBus()
        ambient = {"caller_tier": "pro"}
        token = current_actor_context.set(ambient)
        try:
            bus.publish(EventType.JOB_QUEUED, {"job_id": "j4"})
        finally:
            current_actor_context.reset(token)

        # Mutate the live dict — the event's actor must not reflect this.
        ambient["caller_tier"] = "free"
        ambient["leaked"] = True
        events = bus.recent_events()
        assert events[0].actor == {"caller_tier": "pro"}

    def test_to_dict_includes_actor_when_set(self):
        e = Event(
            type=EventType.JOB_QUEUED,
            data={"job_id": "j5"},
            actor={"caller_tier": "pro"},
        )
        d = e.to_dict()
        assert d["actor"] == {"caller_tier": "pro"}

    def test_to_dict_omits_actor_when_none(self):
        e = Event(type=EventType.JOB_QUEUED, data={"job_id": "j6"})
        d = e.to_dict()
        assert "actor" not in d

    def test_context_propagates_to_background_thread_via_copy_context(self):
        """The pattern :class:`_PrintWatcher.start` uses: capture
        :func:`contextvars.copy_context` in the calling thread, run the
        target inside ``ctx.run(...)`` from the background thread.  This
        test pins that the value actually survives the thread boundary —
        without ``copy_context``, the ContextVar would default to None
        in the child thread and publish-time stamping would silently
        drop the actor.
        """
        import contextvars

        from kiln.events import current_actor_context

        bus = EventBus()
        token = current_actor_context.set({"caller_tier": "pro", "caller_id": "t-9"})
        try:
            ctx = contextvars.copy_context()
        finally:
            current_actor_context.reset(token)

        # ContextVar is back to default in the parent thread now.
        assert current_actor_context.get() is None

        def _publish_from_thread():
            # Inside ctx.run, the ContextVar's value is the one snapshotted above.
            bus.publish(EventType.JOB_QUEUED, {"job_id": "j7"})

        t = threading.Thread(target=lambda: ctx.run(_publish_from_thread))
        t.start()
        t.join(timeout=2)

        events = bus.recent_events()
        assert len(events) == 1
        assert events[0].actor == {"caller_tier": "pro", "caller_id": "t-9"}

    def test_naked_thread_without_copy_context_loses_actor(self):
        """Sanity check / regression guard: a thread that does NOT use
        ``copy_context`` does NOT see the parent thread's ContextVar
        value.  If this ever fails (e.g. Python changes ContextVar
        semantics to auto-propagate), the ``copy_context`` ceremony in
        _PrintWatcher.start becomes unnecessary and the comment there
        can be simplified."""
        from kiln.events import current_actor_context

        bus = EventBus()
        token = current_actor_context.set({"caller_tier": "pro"})
        try:
            def _publish_from_naked_thread():
                # No ctx.run — ContextVar should be at its default here.
                bus.publish(EventType.JOB_QUEUED, {"job_id": "j8"})

            t = threading.Thread(target=_publish_from_naked_thread)
            t.start()
            t.join(timeout=2)
        finally:
            current_actor_context.reset(token)

        events = bus.recent_events()
        assert len(events) == 1
        # Naked thread → ContextVar default (None) → event.actor stays None.
        assert events[0].actor is None
