"""Tests for kiln.webhooks -- webhook delivery system.

Covers:
- Register/unregister endpoints
- List and get endpoints
- Event filtering (specific events vs wildcard)
- HMAC signature computation and header inclusion
- Delivery with retries on failure
- Delivery success on first attempt
- Delivery failure after max retries
- Non-2xx status codes trigger retry
- Start/stop lifecycle
- Delivery record history
- Inactive endpoints skipped
- Empty events set = subscribe to all
- Thread safety of endpoint registration
- to_dict serialization
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import threading
import time
from unittest.mock import MagicMock

import pytest

from kiln.events import Event, EventBus, EventType
from kiln.webhooks import DeliveryRecord, WebhookEndpoint, WebhookManager, _mask_secret, _validate_webhook_url


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_url_validation(monkeypatch):
    """Bypass SSRF URL validation in tests that use mock URLs."""
    monkeypatch.setattr(
        "kiln.webhooks._validate_webhook_url",
        lambda url: (True, ""),
    )


def _make_manager(
    event_bus: EventBus | None = None,
    max_retries: int = 3,
    retry_delay: float = 0.0,
    delivery_timeout: float = 1.0,
) -> WebhookManager:
    """Create a WebhookManager with a zero-delay retry for fast tests."""
    bus = event_bus or EventBus()
    return WebhookManager(
        event_bus=bus,
        max_retries=max_retries,
        retry_delay=retry_delay,
        delivery_timeout=delivery_timeout,
    )


def _inject_sender(manager: WebhookManager, sender: MagicMock) -> None:
    """Inject a mock send function to avoid real HTTP requests."""
    manager._send_func = sender


# ---------------------------------------------------------------------------
# WebhookEndpoint dataclass
# ---------------------------------------------------------------------------

class TestWebhookEndpoint:
    """Tests for the WebhookEndpoint dataclass."""

    def test_to_dict_returns_all_fields(self):
        ep = WebhookEndpoint(
            id="abc123",
            url="https://example.com/hook",
            events={"job.completed", "job.failed"},
            secret="s3cret",
            active=True,
            created_at=1000.0,
            description="My hook",
        )
        d = ep.to_dict()
        assert d["id"] == "abc123"
        assert d["url"] == "https://example.com/hook"
        assert d["events"] == ["job.completed", "job.failed"]  # sorted
        assert d["secret"] == "s3cret"
        assert d["active"] is True
        assert d["created_at"] == 1000.0
        assert d["description"] == "My hook"

    def test_to_dict_events_are_sorted(self):
        ep = WebhookEndpoint(
            id="x",
            url="https://example.com",
            events={"z.event", "a.event", "m.event"},
        )
        d = ep.to_dict()
        assert d["events"] == ["a.event", "m.event", "z.event"]

    def test_to_dict_empty_events(self):
        ep = WebhookEndpoint(id="x", url="https://example.com", events=set())
        d = ep.to_dict()
        assert d["events"] == []

    def test_default_values(self):
        ep = WebhookEndpoint(id="x", url="https://example.com", events=set())
        assert ep.secret is None
        assert ep.active is True
        assert isinstance(ep.created_at, float)
        assert ep.description == ""


# ---------------------------------------------------------------------------
# DeliveryRecord dataclass
# ---------------------------------------------------------------------------

class TestDeliveryRecord:
    """Tests for the DeliveryRecord dataclass."""

    def test_to_dict_returns_all_fields(self):
        rec = DeliveryRecord(
            id="rec1",
            webhook_id="wh1",
            event_type="job.completed",
            url="https://example.com/hook",
            status_code=200,
            success=True,
            error=None,
            attempts=1,
            timestamp=2000.0,
        )
        d = rec.to_dict()
        assert d["id"] == "rec1"
        assert d["webhook_id"] == "wh1"
        assert d["event_type"] == "job.completed"
        assert d["url"] == "https://example.com/hook"
        assert d["status_code"] == 200
        assert d["success"] is True
        assert d["error"] is None
        assert d["attempts"] == 1
        assert d["timestamp"] == 2000.0

    def test_default_values(self):
        rec = DeliveryRecord(
            id="rec1",
            webhook_id="wh1",
            event_type="job.completed",
            url="https://example.com",
        )
        assert rec.status_code is None
        assert rec.success is False
        assert rec.error is None
        assert rec.attempts == 0
        assert isinstance(rec.timestamp, float)


# ---------------------------------------------------------------------------
# Register / Unregister
# ---------------------------------------------------------------------------

class TestRegisterUnregister:
    """Tests for endpoint registration and removal."""

    def test_register_returns_endpoint(self):
        mgr = _make_manager()
        ep = mgr.register(url="https://example.com/hook")
        assert isinstance(ep, WebhookEndpoint)
        assert ep.url == "https://example.com/hook"
        assert len(ep.id) == 12

    def test_register_with_events(self):
        mgr = _make_manager()
        ep = mgr.register(
            url="https://example.com/hook",
            events=["job.completed", "job.failed"],
        )
        assert ep.events == {"job.completed", "job.failed"}

    def test_register_with_no_events_means_all(self):
        mgr = _make_manager()
        ep = mgr.register(url="https://example.com/hook")
        assert ep.events == set()

    def test_register_with_secret(self):
        mgr = _make_manager()
        ep = mgr.register(url="https://example.com/hook", secret="my-secret")
        assert ep.secret == "my-secret"

    def test_register_with_description(self):
        mgr = _make_manager()
        ep = mgr.register(
            url="https://example.com/hook",
            description="Test endpoint",
        )
        assert ep.description == "Test endpoint"

    def test_register_multiple_endpoints(self):
        mgr = _make_manager()
        ep1 = mgr.register(url="https://example.com/hook1")
        ep2 = mgr.register(url="https://example.com/hook2")
        assert ep1.id != ep2.id
        assert len(mgr.list_endpoints()) == 2

    def test_unregister_existing(self):
        mgr = _make_manager()
        ep = mgr.register(url="https://example.com/hook")
        assert mgr.unregister(ep.id) is True
        assert mgr.list_endpoints() == []

    def test_unregister_nonexistent(self):
        mgr = _make_manager()
        assert mgr.unregister("nonexistent") is False

    def test_unregister_does_not_affect_others(self):
        mgr = _make_manager()
        ep1 = mgr.register(url="https://example.com/hook1")
        ep2 = mgr.register(url="https://example.com/hook2")
        mgr.unregister(ep1.id)
        endpoints = mgr.list_endpoints()
        assert len(endpoints) == 1
        assert endpoints[0].id == ep2.id


# ---------------------------------------------------------------------------
# List and Get endpoints
# ---------------------------------------------------------------------------

class TestListAndGetEndpoints:
    """Tests for listing and retrieving endpoints."""

    def test_list_empty(self):
        mgr = _make_manager()
        assert mgr.list_endpoints() == []

    def test_list_returns_all(self):
        mgr = _make_manager()
        mgr.register(url="https://a.com")
        mgr.register(url="https://b.com")
        mgr.register(url="https://c.com")
        assert len(mgr.list_endpoints()) == 3

    def test_get_existing(self):
        mgr = _make_manager()
        ep = mgr.register(url="https://example.com/hook")
        found = mgr.get_endpoint(ep.id)
        assert found is not None
        assert found.id == ep.id
        assert found.url == ep.url

    def test_get_nonexistent(self):
        mgr = _make_manager()
        assert mgr.get_endpoint("nonexistent") is None


# ---------------------------------------------------------------------------
# Event filtering
# ---------------------------------------------------------------------------

class TestEventFiltering:
    """Tests for event type filtering on endpoints."""

    def test_specific_events_filter_match(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        mgr.register(url="https://example.com/hook", events=["job.completed"])
        mgr.start()

        try:
            bus.publish(Event(type=EventType.JOB_COMPLETED, data={"job": "1"}))
            time.sleep(0.2)
            assert sender.call_count == 1
        finally:
            mgr.stop()

    def test_specific_events_filter_no_match(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        mgr.register(url="https://example.com/hook", events=["job.completed"])
        mgr.start()

        try:
            bus.publish(Event(type=EventType.JOB_FAILED, data={"job": "1"}))
            time.sleep(0.2)
            assert sender.call_count == 0
        finally:
            mgr.stop()

    def test_empty_events_subscribes_to_all(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        mgr.register(url="https://example.com/hook")  # no events = all
        mgr.start()

        try:
            bus.publish(Event(type=EventType.JOB_COMPLETED))
            bus.publish(Event(type=EventType.JOB_FAILED))
            bus.publish(Event(type=EventType.PRINT_STARTED))
            time.sleep(0.3)
            assert sender.call_count == 3
        finally:
            mgr.stop()

    def test_multiple_endpoints_receive_matching_events(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        mgr.register(url="https://a.com", events=["job.completed"])
        mgr.register(url="https://b.com", events=["job.completed", "job.failed"])
        mgr.start()

        try:
            bus.publish(Event(type=EventType.JOB_COMPLETED))
            time.sleep(0.2)
            # Both endpoints match job.completed
            assert sender.call_count == 2
        finally:
            mgr.stop()


# ---------------------------------------------------------------------------
# HMAC signature
# ---------------------------------------------------------------------------

class TestHMACSignature:
    """Tests for HMAC-SHA256 signing."""

    def test_compute_signature(self):
        mgr = _make_manager()
        payload = '{"type": "job.completed"}'
        secret = "test-secret"
        sig = mgr.compute_signature(secret, payload)

        expected = "sha256=" + hmac.new(
            secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        assert sig == expected

    def test_signature_header_included_when_secret_set(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus, max_retries=1)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        mgr.register(
            url="https://example.com/hook",
            events=["job.completed"],
            secret="my-secret",
        )
        mgr.start()

        try:
            bus.publish(Event(type=EventType.JOB_COMPLETED, data={"x": 1}))
            time.sleep(0.2)

            assert sender.call_count == 1
            _url, payload, headers, _timeout = sender.call_args[0]
            assert "X-Kiln-Signature" in headers
            assert headers["X-Kiln-Signature"].startswith("sha256=")

            # Verify the signature is correct
            expected_sig = hmac.new(
                b"my-secret", payload.encode(), hashlib.sha256
            ).hexdigest()
            assert headers["X-Kiln-Signature"] == f"sha256={expected_sig}"
        finally:
            mgr.stop()

    def test_no_signature_header_without_secret(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus, max_retries=1)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        mgr.register(url="https://example.com/hook", events=["job.completed"])
        mgr.start()

        try:
            bus.publish(Event(type=EventType.JOB_COMPLETED))
            time.sleep(0.2)

            assert sender.call_count == 1
            _url, _payload, headers, _timeout = sender.call_args[0]
            assert "X-Kiln-Signature" not in headers
        finally:
            mgr.stop()


# ---------------------------------------------------------------------------
# Delivery with retries
# ---------------------------------------------------------------------------

class TestDeliveryRetries:
    """Tests for retry behaviour on delivery failures."""

    def test_success_on_first_attempt(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus, max_retries=3)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        mgr.register(url="https://example.com/hook", events=["job.completed"])
        mgr.start()

        try:
            bus.publish(Event(type=EventType.JOB_COMPLETED))
            time.sleep(0.2)
            assert sender.call_count == 1

            records = mgr.recent_deliveries()
            assert len(records) == 1
            assert records[0].success is True
            assert records[0].attempts == 1
        finally:
            mgr.stop()

    def test_retry_then_success(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus, max_retries=3)
        # Fail twice, succeed on third attempt
        sender = MagicMock(side_effect=[500, 503, 200])
        _inject_sender(mgr, sender)

        mgr.register(url="https://example.com/hook", events=["job.completed"])
        mgr.start()

        try:
            bus.publish(Event(type=EventType.JOB_COMPLETED))
            time.sleep(0.3)
            assert sender.call_count == 3

            records = mgr.recent_deliveries()
            assert len(records) == 1
            assert records[0].success is True
            assert records[0].attempts == 3
            assert records[0].status_code == 200
        finally:
            mgr.stop()

    def test_failure_after_max_retries(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus, max_retries=3)
        sender = MagicMock(return_value=500)
        _inject_sender(mgr, sender)

        mgr.register(url="https://example.com/hook", events=["job.completed"])
        mgr.start()

        try:
            bus.publish(Event(type=EventType.JOB_COMPLETED))
            time.sleep(0.3)
            assert sender.call_count == 3

            records = mgr.recent_deliveries()
            assert len(records) == 1
            assert records[0].success is False
            assert records[0].attempts == 3
            assert records[0].error == "HTTP 500"
        finally:
            mgr.stop()

    def test_exception_triggers_retry(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus, max_retries=3)
        sender = MagicMock(
            side_effect=[ConnectionError("refused"), TimeoutError("timed out"), 200]
        )
        _inject_sender(mgr, sender)

        mgr.register(url="https://example.com/hook", events=["job.completed"])
        mgr.start()

        try:
            bus.publish(Event(type=EventType.JOB_COMPLETED))
            time.sleep(0.3)
            assert sender.call_count == 3

            records = mgr.recent_deliveries()
            assert len(records) == 1
            assert records[0].success is True
            assert records[0].attempts == 3
        finally:
            mgr.stop()

    def test_non_2xx_triggers_retry(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus, max_retries=2)
        sender = MagicMock(side_effect=[404, 200])
        _inject_sender(mgr, sender)

        mgr.register(url="https://example.com/hook", events=["job.completed"])
        mgr.start()

        try:
            bus.publish(Event(type=EventType.JOB_COMPLETED))
            time.sleep(0.2)
            assert sender.call_count == 2

            records = mgr.recent_deliveries()
            assert len(records) == 1
            assert records[0].success is True
            assert records[0].attempts == 2
        finally:
            mgr.stop()

    def test_all_attempts_fail_with_exception(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus, max_retries=2)
        sender = MagicMock(side_effect=ConnectionError("refused"))
        _inject_sender(mgr, sender)

        mgr.register(url="https://example.com/hook", events=["job.completed"])
        mgr.start()

        try:
            bus.publish(Event(type=EventType.JOB_COMPLETED))
            time.sleep(0.2)
            assert sender.call_count == 2

            records = mgr.recent_deliveries()
            assert len(records) == 1
            assert records[0].success is False
            assert records[0].attempts == 2
            assert "refused" in records[0].error
        finally:
            mgr.stop()


# ---------------------------------------------------------------------------
# Start / Stop lifecycle
# ---------------------------------------------------------------------------

class TestStartStopLifecycle:
    """Tests for the manager start/stop lifecycle."""

    def test_is_running_initially_false(self):
        mgr = _make_manager()
        assert mgr.is_running is False

    def test_start_sets_running(self):
        mgr = _make_manager()
        mgr.start()
        try:
            assert mgr.is_running is True
        finally:
            mgr.stop()

    def test_stop_clears_running(self):
        mgr = _make_manager()
        mgr.start()
        mgr.stop()
        assert mgr.is_running is False

    def test_double_start_is_safe(self):
        mgr = _make_manager()
        mgr.start()
        mgr.start()  # should not raise or create a second thread
        try:
            assert mgr.is_running is True
        finally:
            mgr.stop()

    def test_stop_without_start_is_safe(self):
        mgr = _make_manager()
        mgr.stop()  # should not raise
        assert mgr.is_running is False

    def test_events_not_delivered_after_stop(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        mgr.register(url="https://example.com/hook", events=["job.completed"])
        mgr.start()
        mgr.stop()

        bus.publish(Event(type=EventType.JOB_COMPLETED))
        time.sleep(0.2)
        assert sender.call_count == 0

    def test_restart_works(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        mgr.register(url="https://example.com/hook", events=["job.completed"])

        mgr.start()
        bus.publish(Event(type=EventType.JOB_COMPLETED))
        time.sleep(0.2)
        mgr.stop()

        assert sender.call_count == 1

        mgr.start()
        bus.publish(Event(type=EventType.JOB_COMPLETED))
        time.sleep(0.2)
        mgr.stop()

        assert sender.call_count == 2


# ---------------------------------------------------------------------------
# Delivery record history
# ---------------------------------------------------------------------------

class TestDeliveryHistory:
    """Tests for delivery record history tracking."""

    def test_recent_deliveries_empty(self):
        mgr = _make_manager()
        assert mgr.recent_deliveries() == []

    def test_recent_deliveries_records_delivery(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus, max_retries=1)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        mgr.register(url="https://example.com/hook", events=["job.completed"])
        mgr.start()

        try:
            bus.publish(Event(type=EventType.JOB_COMPLETED))
            time.sleep(0.2)

            records = mgr.recent_deliveries()
            assert len(records) == 1
            assert records[0].event_type == "job.completed"
            assert records[0].url == "https://example.com/hook"
            assert records[0].success is True
            assert records[0].status_code == 200
        finally:
            mgr.stop()

    def test_recent_deliveries_newest_first(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus, max_retries=1)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        mgr.register(url="https://example.com/hook")
        mgr.start()

        try:
            bus.publish(Event(type=EventType.JOB_COMPLETED))
            time.sleep(0.1)
            bus.publish(Event(type=EventType.JOB_FAILED))
            time.sleep(0.2)

            records = mgr.recent_deliveries()
            assert len(records) == 2
            # Newest first
            assert records[0].event_type == "job.failed"
            assert records[1].event_type == "job.completed"
        finally:
            mgr.stop()

    def test_recent_deliveries_respects_limit(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus, max_retries=1)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        mgr.register(url="https://example.com/hook")
        mgr.start()

        try:
            for _ in range(5):
                bus.publish(Event(type=EventType.JOB_COMPLETED))
            time.sleep(0.5)

            records = mgr.recent_deliveries(limit=2)
            assert len(records) == 2
        finally:
            mgr.stop()

    def test_history_truncated_at_max(self):
        mgr = _make_manager(max_retries=1)
        mgr._max_history = 5
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        # Deliver directly (bypass event bus) to fill history
        for i in range(10):
            event = Event(
                type=EventType.JOB_COMPLETED,
                data={"i": i},
            )
            endpoint = WebhookEndpoint(
                id=f"ep{i}",
                url="https://example.com",
                events=set(),
            )
            mgr._deliver(endpoint, event)

        records = mgr.recent_deliveries(limit=100)
        assert len(records) == 5


# ---------------------------------------------------------------------------
# Inactive endpoints
# ---------------------------------------------------------------------------

class TestInactiveEndpoints:
    """Tests for inactive endpoint handling."""

    def test_inactive_endpoint_skipped(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        ep = mgr.register(url="https://example.com/hook", events=["job.completed"])
        ep.active = False
        mgr.start()

        try:
            bus.publish(Event(type=EventType.JOB_COMPLETED))
            time.sleep(0.2)
            assert sender.call_count == 0
        finally:
            mgr.stop()

    def test_mix_of_active_and_inactive(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        ep1 = mgr.register(url="https://active.com", events=["job.completed"])
        ep2 = mgr.register(url="https://inactive.com", events=["job.completed"])
        ep2.active = False
        mgr.start()

        try:
            bus.publish(Event(type=EventType.JOB_COMPLETED))
            time.sleep(0.2)
            assert sender.call_count == 1
            called_url = sender.call_args[0][0]
            assert called_url == "https://active.com"
        finally:
            mgr.stop()


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety:
    """Tests for thread-safe endpoint registration."""

    def test_concurrent_register(self):
        mgr = _make_manager()
        errors: list[Exception] = []

        def register_batch(start: int) -> None:
            try:
                for i in range(20):
                    mgr.register(url=f"https://example.com/hook-{start}-{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=register_batch, args=(n,))
            for n in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(mgr.list_endpoints()) == 100

    def test_concurrent_register_and_unregister(self):
        mgr = _make_manager()
        registered_ids: list[str] = []
        lock = threading.Lock()

        def register_batch() -> None:
            for i in range(20):
                ep = mgr.register(url=f"https://example.com/{i}")
                with lock:
                    registered_ids.append(ep.id)

        def unregister_batch() -> None:
            time.sleep(0.01)
            with lock:
                ids_to_remove = list(registered_ids)
            for eid in ids_to_remove:
                mgr.unregister(eid)

        t1 = threading.Thread(target=register_batch)
        t2 = threading.Thread(target=unregister_batch)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # No crash = success; exact count depends on timing

    def test_concurrent_delivery_and_register(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus, max_retries=1)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)
        errors: list[Exception] = []

        mgr.register(url="https://existing.com")
        mgr.start()

        def publish_events() -> None:
            try:
                for _ in range(20):
                    bus.publish(Event(type=EventType.JOB_COMPLETED))
                    time.sleep(0.01)
            except Exception as exc:
                errors.append(exc)

        def register_endpoints() -> None:
            try:
                for i in range(20):
                    mgr.register(url=f"https://new-{i}.com")
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=publish_events)
        t2 = threading.Thread(target=register_endpoints)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        try:
            assert len(errors) == 0
        finally:
            mgr.stop()


# ---------------------------------------------------------------------------
# Payload correctness
# ---------------------------------------------------------------------------

class TestPayloadCorrectness:
    """Tests that the delivered payload is correct JSON."""

    def test_payload_contains_event_data(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus, max_retries=1)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        mgr.register(url="https://example.com/hook", events=["job.completed"])
        mgr.start()

        try:
            event = Event(
                type=EventType.JOB_COMPLETED,
                data={"job_id": "abc123", "printer": "voron"},
                source="test",
            )
            bus.publish(event)
            time.sleep(0.2)

            assert sender.call_count == 1
            _url, payload_str, _headers, _timeout = sender.call_args[0]
            payload = json.loads(payload_str)
            assert payload["type"] == "job.completed"
            assert payload["data"]["job_id"] == "abc123"
            assert payload["data"]["printer"] == "voron"
            assert payload["source"] == "test"
            assert "timestamp" in payload
        finally:
            mgr.stop()

    def test_content_type_header_is_json(self):
        bus = EventBus()
        mgr = _make_manager(event_bus=bus, max_retries=1)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        mgr.register(url="https://example.com/hook", events=["job.completed"])
        mgr.start()

        try:
            bus.publish(Event(type=EventType.JOB_COMPLETED))
            time.sleep(0.2)

            _url, _payload, headers, _timeout = sender.call_args[0]
            assert headers["Content-Type"] == "application/json"
        finally:
            mgr.stop()


# ---------------------------------------------------------------------------
# Direct _deliver method tests (bypass event bus)
# ---------------------------------------------------------------------------

class TestDirectDeliver:
    """Tests using _deliver directly for deterministic behaviour."""

    def test_deliver_success(self):
        mgr = _make_manager(max_retries=1)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        endpoint = WebhookEndpoint(
            id="ep1",
            url="https://example.com/hook",
            events={"job.completed"},
        )
        event = Event(type=EventType.JOB_COMPLETED, data={"x": 1})
        record = mgr._deliver(endpoint, event)

        assert record.success is True
        assert record.status_code == 200
        assert record.attempts == 1

    def test_deliver_with_signature(self):
        mgr = _make_manager(max_retries=1)
        sender = MagicMock(return_value=200)
        _inject_sender(mgr, sender)

        endpoint = WebhookEndpoint(
            id="ep1",
            url="https://example.com/hook",
            events={"job.completed"},
            secret="test-secret",
        )
        event = Event(type=EventType.JOB_COMPLETED)
        mgr._deliver(endpoint, event)

        _url, payload, headers, _timeout = sender.call_args[0]
        assert "X-Kiln-Signature" in headers

        expected = hmac.new(
            b"test-secret", payload.encode(), hashlib.sha256
        ).hexdigest()
        assert headers["X-Kiln-Signature"] == f"sha256={expected}"

    def test_deliver_retries_on_500(self):
        mgr = _make_manager(max_retries=3)
        sender = MagicMock(side_effect=[500, 502, 200])
        _inject_sender(mgr, sender)

        endpoint = WebhookEndpoint(
            id="ep1",
            url="https://example.com/hook",
            events=set(),
        )
        event = Event(type=EventType.JOB_COMPLETED)
        record = mgr._deliver(endpoint, event)

        assert record.success is True
        assert record.attempts == 3
        assert sender.call_count == 3

    def test_deliver_records_last_error_on_failure(self):
        mgr = _make_manager(max_retries=2)
        sender = MagicMock(return_value=503)
        _inject_sender(mgr, sender)

        endpoint = WebhookEndpoint(
            id="ep1",
            url="https://example.com/hook",
            events=set(),
        )
        event = Event(type=EventType.JOB_COMPLETED)
        record = mgr._deliver(endpoint, event)

        assert record.success is False
        assert record.error == "HTTP 503"
        assert record.attempts == 2

    def test_deliver_records_exception_error(self):
        mgr = _make_manager(max_retries=1)
        sender = MagicMock(side_effect=ConnectionError("connection refused"))
        _inject_sender(mgr, sender)

        endpoint = WebhookEndpoint(
            id="ep1",
            url="https://example.com/hook",
            events=set(),
        )
        event = Event(type=EventType.JOB_COMPLETED)
        record = mgr._deliver(endpoint, event)

        assert record.success is False
        assert "connection refused" in record.error

    def test_deliver_2xx_range(self):
        """Any 2xx status should be treated as success."""
        for code in [200, 201, 202, 204]:
            mgr = _make_manager(max_retries=1)
            sender = MagicMock(return_value=code)
            _inject_sender(mgr, sender)

            endpoint = WebhookEndpoint(
                id="ep1",
                url="https://example.com/hook",
                events=set(),
            )
            event = Event(type=EventType.JOB_COMPLETED)
            record = mgr._deliver(endpoint, event)

            assert record.success is True, f"Expected success for HTTP {code}"
            assert record.status_code == code


# ---------------------------------------------------------------------------
# _validate_webhook_url (SSRF prevention)
# ---------------------------------------------------------------------------

class TestValidateWebhookUrl:
    """Tests for the _validate_webhook_url SSRF validation function.

    These tests intentionally do NOT use the autouse _bypass_url_validation
    fixture, so they exercise the real validation logic.
    """

    @pytest.fixture(autouse=True)
    def _restore_real_validation(self, monkeypatch):
        """Undo the module-level bypass so we test the real function."""
        # Import the real function before the monkeypatch restores it
        from kiln.webhooks import _validate_webhook_url as real_fn
        monkeypatch.setattr("kiln.webhooks._validate_webhook_url", real_fn)

    def test_valid_https_url(self, monkeypatch):
        monkeypatch.setattr(
            "kiln.webhooks.socket.getaddrinfo",
            lambda _host, port, proto=socket.IPPROTO_TCP: [
                (socket.AF_INET, socket.SOCK_STREAM, proto, "", ("93.184.216.34", port))
            ],
        )
        valid, reason = _validate_webhook_url("https://example.com/hook")
        assert valid is True
        assert reason == ""

    def test_rejects_http_url_by_default(self, monkeypatch):
        monkeypatch.setattr(
            "kiln.webhooks.socket.getaddrinfo",
            lambda _host, port, proto=socket.IPPROTO_TCP: [
                (socket.AF_INET, socket.SOCK_STREAM, proto, "", ("93.184.216.34", port))
            ],
        )
        valid, reason = _validate_webhook_url("http://example.com/hook")
        assert valid is False
        assert "http" in reason.lower()

    def test_allows_http_url_when_opted_in(self, monkeypatch):
        monkeypatch.setenv("KILN_WEBHOOK_ALLOW_HTTP", "1")
        monkeypatch.setattr(
            "kiln.webhooks.socket.getaddrinfo",
            lambda _host, port, proto=socket.IPPROTO_TCP: [
                (socket.AF_INET, socket.SOCK_STREAM, proto, "", ("93.184.216.34", port))
            ],
        )
        valid, reason = _validate_webhook_url("http://example.com/hook")
        assert valid is True
        assert reason == ""

    def test_rejects_ftp_scheme(self):
        valid, reason = _validate_webhook_url("ftp://example.com/file")
        assert valid is False
        assert "scheme" in reason.lower()

    def test_rejects_file_scheme(self):
        valid, reason = _validate_webhook_url("file:///etc/passwd")
        assert valid is False
        assert "scheme" in reason.lower()

    def test_rejects_empty_scheme(self):
        valid, reason = _validate_webhook_url("://example.com")
        assert valid is False

    def test_rejects_localhost(self):
        valid, reason = _validate_webhook_url("https://localhost/hook")
        assert valid is False
        assert "localhost" in reason.lower()

    def test_rejects_localhost_case_insensitive(self):
        valid, reason = _validate_webhook_url("https://LOCALHOST/hook")
        assert valid is False
        assert "localhost" in reason.lower()

    def test_rejects_loopback_ip(self):
        valid, reason = _validate_webhook_url("https://127.0.0.1/hook")
        assert valid is False
        assert "private" in reason.lower() or "reserved" in reason.lower()

    def test_rejects_loopback_ip_variant(self):
        valid, reason = _validate_webhook_url("https://127.0.0.2/hook")
        assert valid is False
        assert "private" in reason.lower() or "reserved" in reason.lower()

    def test_rejects_private_10_network(self):
        valid, reason = _validate_webhook_url("https://10.0.0.1/hook")
        assert valid is False
        assert "private" in reason.lower() or "reserved" in reason.lower()

    def test_rejects_private_172_network(self):
        valid, reason = _validate_webhook_url("https://172.16.0.1/hook")
        assert valid is False
        assert "private" in reason.lower() or "reserved" in reason.lower()

    def test_rejects_private_192_network(self):
        valid, reason = _validate_webhook_url("https://192.168.1.1/hook")
        assert valid is False
        assert "private" in reason.lower() or "reserved" in reason.lower()

    def test_rejects_link_local(self):
        valid, reason = _validate_webhook_url("https://169.254.1.1/hook")
        assert valid is False
        assert "private" in reason.lower() or "reserved" in reason.lower()

    def test_rejects_ipv6_loopback(self):
        valid, reason = _validate_webhook_url("https://[::1]/hook")
        assert valid is False
        assert "private" in reason.lower() or "reserved" in reason.lower()

    def test_rejects_no_hostname(self):
        valid, reason = _validate_webhook_url("https:///path")
        assert valid is False
        assert "hostname" in reason.lower()

    def test_rejects_embedded_credentials(self):
        valid, reason = _validate_webhook_url("https://user:pass@example.com/hook")
        assert valid is False
        assert "credentials" in reason.lower()

    def test_rejects_unresolvable_hostname(self):
        valid, reason = _validate_webhook_url("https://this-domain-does-not-exist-9999.example/hook")
        assert valid is False
        assert "dns" in reason.lower() or "resolution" in reason.lower()

    def test_returns_tuple(self):
        result = _validate_webhook_url("https://example.com")
        assert isinstance(result, tuple)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# _default_send redirect hardening
# ---------------------------------------------------------------------------


class TestDefaultSendRedirectPolicy:
    """Tests for redirect handling and per-hop validation in _default_send."""

    @pytest.fixture(autouse=True)
    def _restore_real_validation(self, monkeypatch):
        from kiln.webhooks import _validate_webhook_url as real_fn
        monkeypatch.setattr("kiln.webhooks._validate_webhook_url", real_fn)

    def _mock_response(self, status_code: int, location: str | None = None):
        resp = MagicMock()
        resp.status_code = status_code
        headers = {}
        if location is not None:
            headers["Location"] = location
        resp.headers = headers
        return resp

    def test_redirect_blocked_by_default(self, monkeypatch):
        monkeypatch.delenv("KILN_WEBHOOK_ALLOW_REDIRECTS", raising=False)
        monkeypatch.setattr(
            "requests.post",
            MagicMock(return_value=self._mock_response(302, "https://example.com/next")),
        )
        with pytest.raises(RuntimeError, match="redirect blocked by policy"):
            WebhookManager._default_send(
                "https://example.com/hook",
                payload="{}",
                headers={"Content-Type": "application/json"},
                timeout=1.0,
            )

    def test_redirect_allowed_revalidates_each_hop(self, monkeypatch):
        monkeypatch.setenv("KILN_WEBHOOK_ALLOW_REDIRECTS", "1")
        monkeypatch.setenv("KILN_WEBHOOK_MAX_REDIRECTS", "3")
        validate = MagicMock(return_value=(True, ""))
        monkeypatch.setattr("kiln.webhooks._validate_webhook_url", validate)
        monkeypatch.setattr(
            "requests.post",
            MagicMock(side_effect=[
                self._mock_response(302, "https://example.com/next"),
                self._mock_response(200),
            ]),
        )
        code = WebhookManager._default_send(
            "https://example.com/hook",
            payload="{}",
            headers={"Content-Type": "application/json"},
            timeout=1.0,
        )
        assert code == 200
        assert validate.call_count == 2

    def test_redirect_blocks_https_to_http_downgrade(self, monkeypatch):
        monkeypatch.setenv("KILN_WEBHOOK_ALLOW_REDIRECTS", "1")
        monkeypatch.setattr(
            "requests.post",
            MagicMock(return_value=self._mock_response(302, "http://example.com/next")),
        )
        with pytest.raises(RuntimeError, match="downgrade"):
            WebhookManager._default_send(
                "https://example.com/hook",
                payload="{}",
                headers={"Content-Type": "application/json"},
                timeout=1.0,
            )

    def test_runtime_url_validation_failure_blocks_delivery(self, monkeypatch):
        monkeypatch.setattr(
            "kiln.webhooks._validate_webhook_url",
            MagicMock(return_value=(False, "blocked for test")),
        )
        with pytest.raises(RuntimeError, match="blocked for test"):
            WebhookManager._default_send(
                "https://example.com/hook",
                payload="{}",
                headers={"Content-Type": "application/json"},
                timeout=1.0,
            )


# ---------------------------------------------------------------------------
# _mask_secret
# ---------------------------------------------------------------------------

class TestMaskSecret:
    """Tests for the _mask_secret helper."""

    def test_none_returns_none(self):
        assert _mask_secret(None) is None

    def test_short_secret_fully_masked(self):
        assert _mask_secret("abc") == "****"

    def test_exactly_four_chars_fully_masked(self):
        assert _mask_secret("abcd") == "****"

    def test_longer_secret_shows_last_four(self):
        assert _mask_secret("my-signing-secret") == "****cret"

    def test_five_char_secret(self):
        assert _mask_secret("12345") == "****2345"

    def test_empty_string(self):
        assert _mask_secret("") == "****"


# ---------------------------------------------------------------------------
# Secret masking in list_endpoints / get_endpoint
# ---------------------------------------------------------------------------

class TestSecretMasking:
    """Verify that list_endpoints and get_endpoint mask secrets."""

    def test_list_endpoints_masks_secret(self):
        mgr = _make_manager()
        ep = mgr.register(url="https://example.com/hook", secret="super-secret-key")
        listed = mgr.list_endpoints()
        assert len(listed) == 1
        assert listed[0].secret == "****-key"
        # Verify the internal copy still has the real secret
        assert mgr._endpoints[ep.id].secret == "super-secret-key"

    def test_list_endpoints_none_secret_stays_none(self):
        mgr = _make_manager()
        mgr.register(url="https://example.com/hook")
        listed = mgr.list_endpoints()
        assert listed[0].secret is None

    def test_get_endpoint_masks_secret(self):
        mgr = _make_manager()
        ep = mgr.register(url="https://example.com/hook", secret="another-secret")
        fetched = mgr.get_endpoint(ep.id)
        assert fetched is not None
        assert fetched.secret == "****cret"
        # Internal copy preserved
        assert mgr._endpoints[ep.id].secret == "another-secret"

    def test_get_endpoint_nonexistent(self):
        mgr = _make_manager()
        assert mgr.get_endpoint("nope") is None
