"""Webhook delivery -- notify external services of Kiln events.

Allows users and integrations to register HTTP endpoints that receive
POST requests whenever certain events occur (job completed, print failed,
temperature warning, etc.).

Delivery is best-effort with configurable retries. Failed deliveries
are logged but do not block the event pipeline.

Example::

    hooks = WebhookManager(event_bus)
    hooks.register(
        url="https://example.com/kiln-events",
        events=["job.completed", "job.failed"],
        secret="my-signing-secret",
    )
    hooks.start()  # begins listening and delivering
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import queue
import socket
import threading
import time
import urllib.parse
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from kiln.events import Event, EventBus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSRF prevention: private/reserved IP networks that webhooks must not target
# ---------------------------------------------------------------------------
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]
_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
_MAX_REDIRECT_HOPS_DEFAULT = 3


def _env_truthy(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable."""
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return default
    return raw in {"1", "true", "yes", "on"}


def _get_webhook_redirect_policy() -> tuple[bool, int]:
    """Return (allow_redirects, max_hops) policy from environment."""
    allow = _env_truthy("KILN_WEBHOOK_ALLOW_REDIRECTS", default=False)
    raw_hops = os.environ.get("KILN_WEBHOOK_MAX_REDIRECTS", str(_MAX_REDIRECT_HOPS_DEFAULT)).strip()
    try:
        max_hops = int(raw_hops)
    except ValueError:
        max_hops = _MAX_REDIRECT_HOPS_DEFAULT
    max_hops = max(0, min(max_hops, 10))
    return allow, max_hops


def _validate_webhook_url(url: str) -> tuple[bool, str]:
    """Validate a webhook URL to prevent SSRF attacks.

    Checks:
    - Scheme must be http or https.
    - Hostname must not be ``localhost``.
    - Resolved IP addresses must not fall within private/reserved ranges.

    Returns:
        A ``(valid, reason)`` tuple.  When *valid* is ``False``, *reason*
        contains a human-readable explanation.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False, "Malformed URL"

    # -- Scheme check --
    if parsed.scheme not in ("http", "https"):
        return False, f"Unsupported URL scheme '{parsed.scheme}'; only http and https are allowed"

    # -- Hostname presence --
    hostname = parsed.hostname
    if not hostname:
        return False, "URL has no hostname"

    # -- Embedded credentials are not allowed --
    if parsed.username or parsed.password:
        return False, "Webhook URLs must not include embedded credentials"

    # -- Explicit localhost block --
    if hostname.lower() in ("localhost",):
        return False, "Webhook URLs must not target localhost"

    # -- Resolve and check IP ranges --
    try:
        addrinfos = socket.getaddrinfo(hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return False, f"DNS resolution failed for '{hostname}': {exc}"

    for _family, _type, _proto, _canonname, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        for network in _BLOCKED_NETWORKS:
            if addr in network:
                return False, (
                    f"Webhook URL resolves to private/reserved address {ip_str} (in {network}); this is not allowed"
                )

    return True, ""


def _mask_secret(secret: str | None) -> str | None:
    """Return a masked version of a webhook secret for display.

    Shows only the last 4 characters, prefixed with asterisks.
    Returns ``None`` if the input is ``None``.
    """
    if secret is None:
        return None
    if len(secret) <= 4:
        return "****"
    return "****" + secret[-4:]


@dataclass
class WebhookEndpoint:
    """A registered webhook endpoint."""

    id: str
    url: str
    events: set[str]  # set of event type values like "job.completed"
    secret: str | None = None  # HMAC signing secret
    active: bool = True
    created_at: float = field(default_factory=time.time)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["events"] = sorted(self.events)
        return data


@dataclass
class DeliveryRecord:
    """Record of a webhook delivery attempt."""

    id: str
    webhook_id: str
    event_type: str
    url: str
    status_code: int | None = None
    success: bool = False
    error: str | None = None
    attempts: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WebhookManager:
    """Manages webhook registrations and event delivery.

    Uses a background thread with a delivery queue for async,
    non-blocking delivery. Supports HMAC-SHA256 signing for
    webhook verification.
    """

    def __init__(
        self,
        event_bus: EventBus,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        delivery_timeout: float = 10.0,
    ) -> None:
        self._event_bus = event_bus
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._delivery_timeout = delivery_timeout

        self._endpoints: dict[str, WebhookEndpoint] = {}
        self._delivery_history: list[DeliveryRecord] = []
        self._max_history = 500
        self._lock = threading.Lock()

        self._delivery_queue: queue.Queue = queue.Queue(maxsize=10_000)
        self._running = False
        self._thread: threading.Thread | None = None
        self._handler_ref: Any | None = None

        # HTTP sender (injectable for testing)
        self._send_func = self._default_send

        # Dead-letter queue for events that fail all retries
        self._dead_letters: list[dict[str, Any]] = []
        self._max_dead_letters = 1000

    @property
    def is_running(self) -> bool:
        return self._running

    # -- Registration ------------------------------------------------------

    def register(
        self,
        url: str,
        events: list[str] | None = None,
        secret: str | None = None,
        description: str = "",
    ) -> WebhookEndpoint:
        """Register a new webhook endpoint.

        Args:
            url: The HTTP(S) URL to POST events to.
            events: List of event type values to subscribe to.
                If None or empty, subscribes to ALL events.
            secret: Optional HMAC-SHA256 signing secret.
            description: Human-readable description.

        Returns:
            The created WebhookEndpoint.

        Raises:
            ValueError: If the URL fails SSRF validation (private IP,
                bad scheme, unresolvable hostname, etc.).
        """
        valid, reason = _validate_webhook_url(url)
        if not valid:
            raise ValueError(f"Invalid webhook URL: {reason}")

        endpoint_id = uuid.uuid4().hex[:12]
        event_set = set(events) if events else set()

        endpoint = WebhookEndpoint(
            id=endpoint_id,
            url=url,
            events=event_set,
            secret=secret,
            description=description,
        )
        with self._lock:
            self._endpoints[endpoint_id] = endpoint
        logger.info("Registered webhook %s -> %s", endpoint_id, url)
        return endpoint

    def unregister(self, endpoint_id: str) -> bool:
        """Remove a webhook endpoint.

        Returns True if the endpoint existed and was removed.
        """
        with self._lock:
            if endpoint_id in self._endpoints:
                del self._endpoints[endpoint_id]
                return True
            return False

    def list_endpoints(self) -> list[WebhookEndpoint]:
        """Return all registered endpoints with secrets masked.

        The returned copies have their ``secret`` field replaced with
        a masked version (e.g. ``"****abcd"``).  The real secrets are
        preserved internally for HMAC signing.
        """
        with self._lock:
            results: list[WebhookEndpoint] = []
            for ep in self._endpoints.values():
                masked = copy.copy(ep)
                masked.secret = _mask_secret(ep.secret)
                results.append(masked)
            return results

    def get_endpoint(self, endpoint_id: str) -> WebhookEndpoint | None:
        """Return a specific endpoint by ID, with the secret masked."""
        with self._lock:
            ep = self._endpoints.get(endpoint_id)
            if ep is None:
                return None
            masked = copy.copy(ep)
            masked.secret = _mask_secret(ep.secret)
            return masked

    # -- Delivery ----------------------------------------------------------

    def start(self) -> None:
        """Start listening to the event bus and delivering webhooks."""
        if self._running:
            return
        self._running = True
        # Store bound method ref so unsubscribe's identity check works
        self._handler_ref = self._on_event
        self._event_bus.subscribe(None, self._handler_ref)  # wildcard
        self._thread = threading.Thread(
            target=self._delivery_loop,
            name="kiln-webhooks",
            daemon=True,
        )
        self._thread.start()
        logger.info("Webhook manager started")

    def stop(self) -> None:
        """Stop the webhook delivery thread."""
        self._running = False
        self._event_bus.unsubscribe(None, self._handler_ref)
        if self._thread is not None:
            self._delivery_queue.put(None)  # sentinel to unblock
            self._thread.join(timeout=5.0)
            self._thread = None
        # Drain any leftover items so they don't leak into the next start()
        while not self._delivery_queue.empty():
            try:
                self._delivery_queue.get_nowait()
            except queue.Empty:
                break
        logger.info("Webhook manager stopped")

    def _on_event(self, event: Event) -> None:
        """EventBus callback -- enqueue matching deliveries."""
        with self._lock:
            endpoints = list(self._endpoints.values())

        event_value = event.type.value
        for endpoint in endpoints:
            if not endpoint.active:
                continue
            # Empty events set = subscribe to all
            if endpoint.events and event_value not in endpoint.events:
                continue
            try:
                self._delivery_queue.put_nowait((endpoint, event))
            except queue.Full:
                logger.warning(
                    "Webhook delivery queue full, dropping event %s for endpoint %s",
                    event_value,
                    endpoint.url,
                )

    def _delivery_loop(self) -> None:
        """Background delivery worker."""
        while self._running:
            try:
                item = self._delivery_queue.get(timeout=1.0)
                if item is None:
                    break
                endpoint, event = item
                self._deliver(endpoint, event)
            except queue.Empty:
                continue
            except Exception:
                logger.exception("Webhook delivery error")

    def _deliver(self, endpoint: WebhookEndpoint, event: Event) -> DeliveryRecord:
        """Attempt to deliver an event to an endpoint with retries."""
        event_data = event.to_dict()
        event_data["event_id"] = uuid.uuid4().hex
        payload = json.dumps(event_data, default=str)
        headers = {"Content-Type": "application/json"}

        if endpoint.secret:
            signature = hmac.new(
                endpoint.secret.encode(),
                payload.encode(),
                hashlib.sha256,
            ).hexdigest()
            headers["X-Kiln-Signature"] = f"sha256={signature}"

        record = DeliveryRecord(
            id=uuid.uuid4().hex[:12],
            webhook_id=endpoint.id,
            event_type=event.type.value,
            url=endpoint.url,
        )

        for attempt in range(1, self._max_retries + 1):
            record.attempts = attempt
            try:
                status_code = self._send_func(endpoint.url, payload, headers, self._delivery_timeout)
                record.status_code = status_code
                if 200 <= status_code < 300:
                    record.success = True
                    break
                else:
                    record.error = f"HTTP {status_code}"
            except Exception as exc:
                record.error = str(exc)

            if attempt < self._max_retries:
                time.sleep(self._retry_delay)

        with self._lock:
            self._delivery_history.append(record)
            if len(self._delivery_history) > self._max_history:
                self._delivery_history = self._delivery_history[-self._max_history :]

        if record.success:
            logger.debug(
                "Delivered %s to %s (attempt %d)",
                event.type.value,
                endpoint.url,
                record.attempts,
            )
        else:
            logger.warning(
                "Failed to deliver %s to %s after %d attempts: %s",
                event.type.value,
                endpoint.url,
                record.attempts,
                record.error,
            )
            # Add to dead-letter list for later inspection
            dead_entry: dict[str, Any] = {
                "event_id": event_data["event_id"],
                "event_type": event.type.value,
                "webhook_id": endpoint.id,
                "url": endpoint.url,
                "error": record.error,
                "attempts": record.attempts,
                "timestamp": record.timestamp,
            }
            with self._lock:
                self._dead_letters.append(dead_entry)
                if len(self._dead_letters) > self._max_dead_letters:
                    self._dead_letters = self._dead_letters[-self._max_dead_letters :]
            logger.info(
                "Dead-lettered event %s for webhook %s (%d total)",
                event_data["event_id"],
                endpoint.id,
                len(self._dead_letters),
            )

        return record

    def get_dead_letters(self) -> list[dict[str, Any]]:
        """Return the dead-letter list (failed deliveries after all retries)."""
        with self._lock:
            return list(self._dead_letters)

    @property
    def dead_letter_count(self) -> int:
        """Return the number of dead-lettered events."""
        with self._lock:
            return len(self._dead_letters)

    def recent_deliveries(self, limit: int = 50) -> list[DeliveryRecord]:
        """Return recent delivery records, newest first."""
        with self._lock:
            records = list(self._delivery_history)
        records.reverse()
        return records[:limit]

    def compute_signature(self, secret: str, payload: str) -> str:
        """Compute HMAC-SHA256 signature for verification."""
        return "sha256=" + hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _default_send(url: str, payload: str, headers: dict[str, str], timeout: float) -> int:
        """Default HTTP sender using requests with SSRF-safe redirect handling."""
        import requests

        allow_redirects, max_hops = _get_webhook_redirect_policy()
        current_url = url
        hops = 0

        while True:
            # Revalidate every outbound URL to defend against DNS rebinding and redirect pivots.
            valid, reason = _validate_webhook_url(current_url)
            if not valid:
                raise RuntimeError(f"Webhook delivery blocked by URL validation for {current_url!r}: {reason}")

            resp = requests.post(
                current_url,
                data=payload,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
            )

            if resp.status_code not in _REDIRECT_STATUS_CODES:
                return resp.status_code

            if not allow_redirects:
                raise RuntimeError(
                    "Webhook redirect blocked by policy. "
                    "Set KILN_WEBHOOK_ALLOW_REDIRECTS=1 to allow redirects "
                    "with per-hop SSRF validation."
                )
            if hops >= max_hops:
                raise RuntimeError(f"Webhook redirect limit exceeded (max {max_hops} hops).")

            location = resp.headers.get("Location", "")
            if not location:
                return resp.status_code

            next_url = urllib.parse.urljoin(current_url, location)
            current_scheme = urllib.parse.urlparse(current_url).scheme.lower()
            next_scheme = urllib.parse.urlparse(next_url).scheme.lower()
            if current_scheme == "https" and next_scheme == "http":
                raise RuntimeError("Webhook redirect blocked: HTTPS to HTTP downgrade is not allowed.")

            current_url = next_url
            hops += 1
