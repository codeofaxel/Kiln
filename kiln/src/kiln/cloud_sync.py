"""Cloud sync for Kiln.

Synchronises printer configurations, job history, and events to a
remote REST API.  Runs as a background daemon thread that periodically
pushes local SQLite changes and optionally pulls remote config.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SyncConfig:
    """Configuration for cloud sync."""

    cloud_url: str = ""
    api_key: str = ""
    sync_interval_seconds: float = 60.0
    sync_jobs: bool = True
    sync_events: bool = True
    sync_printers: bool = True
    sync_settings: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Mask the API key in output
        if d.get("api_key"):
            d["api_key"] = d["api_key"][:8] + "..."
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SyncConfig:
        return cls(**{
            k: data[k] for k in cls.__dataclass_fields__ if k in data
        })


@dataclass
class SyncStatus:
    """Current state of the sync system."""

    enabled: bool
    connected: bool = False
    last_sync_at: Optional[float] = None
    last_sync_status: str = "never"
    jobs_synced: int = 0
    events_synced: int = 0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# HMAC helper
# ---------------------------------------------------------------------------

def _compute_signature(secret: str, payload: bytes) -> str:
    """Compute HMAC-SHA256 signature for a payload."""
    return hmac.new(
        secret.encode(), payload, hashlib.sha256,
    ).hexdigest()


# ---------------------------------------------------------------------------
# Cloud sync manager
# ---------------------------------------------------------------------------

class CloudSyncManager:
    """Background sync engine.

    Parameters:
        db: :class:`~kiln.persistence.KilnDB` instance.
        event_bus: Optional :class:`~kiln.events.EventBus`.
        config: Initial :class:`SyncConfig`.
    """

    def __init__(
        self,
        db: Any = None,
        event_bus: Any = None,
        config: Optional[SyncConfig] = None,
    ) -> None:
        self._db = db
        self._bus = event_bus
        self._config = config or SyncConfig()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._session = requests.Session()

        # Counters
        self._jobs_synced = 0
        self._events_synced = 0
        self._last_sync_at: Optional[float] = None
        self._last_status = "never"
        self._errors: List[str] = []

    @property
    def enabled(self) -> bool:
        return bool(self._config.cloud_url and self._config.api_key)

    def configure(self, config: SyncConfig) -> None:
        """Update the sync configuration."""
        with self._lock:
            self._config = config
        # Persist to DB settings
        if self._db is not None:
            self._db.set_setting("cloud_sync_config", json.dumps(asdict(config)))

    def start(self) -> None:
        """Start the background sync thread."""
        if not self.enabled:
            logger.info("Cloud sync not configured, skipping start")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="kiln-cloud-sync",
        )
        self._thread.start()
        logger.info("Cloud sync started (interval=%.0fs)", self._config.sync_interval_seconds)

    def stop(self) -> None:
        """Stop the background sync thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None
        logger.info("Cloud sync stopped")

    def status(self) -> SyncStatus:
        """Return current sync status."""
        with self._lock:
            return SyncStatus(
                enabled=self.enabled,
                connected=self._last_status == "success",
                last_sync_at=self._last_sync_at,
                last_sync_status=self._last_status,
                jobs_synced=self._jobs_synced,
                events_synced=self._events_synced,
                errors=list(self._errors[-10:]),
            )

    def sync_now(self) -> Dict[str, Any]:
        """Run a single sync cycle immediately."""
        return self._sync_cycle()

    # -- internal -------------------------------------------------------

    def _run_loop(self) -> None:
        """Main sync loop."""
        while not self._stop_event.is_set():
            try:
                self._sync_cycle()
            except Exception:
                logger.exception("Sync cycle error")
            self._stop_event.wait(timeout=self._config.sync_interval_seconds)

    def _sync_cycle(self) -> Dict[str, Any]:
        """Execute one push/pull sync cycle."""
        if not self.enabled or self._db is None:
            return {"error": "Sync not configured"}

        result: Dict[str, Any] = {"jobs_pushed": 0, "events_pushed": 0}

        # Get sync cursor (last sync timestamp)
        cursor_str = self._db.get_setting("sync_cursor", "0")
        cursor = float(cursor_str) if cursor_str else 0.0

        try:
            # Push jobs
            if self._config.sync_jobs:
                jobs = self._db.get_unsynced_jobs(cursor)
                if jobs:
                    self._push("jobs", jobs)
                    self._db.mark_synced("job", [j["id"] for j in jobs])
                    result["jobs_pushed"] = len(jobs)
                    with self._lock:
                        self._jobs_synced += len(jobs)

            # Push events
            if self._config.sync_events:
                events = self._db.get_unsynced_events(cursor)
                if events:
                    self._push("events", events)
                    self._db.mark_synced(
                        "event", [str(e["id"]) for e in events],
                    )
                    result["events_pushed"] = len(events)
                    with self._lock:
                        self._events_synced += len(events)

            # Push printers
            if self._config.sync_printers:
                printers = self._db.list_printers()
                if printers:
                    self._push("printers", printers)

            # Update cursor
            self._db.set_setting("sync_cursor", str(time.time()))

            with self._lock:
                self._last_sync_at = time.time()
                self._last_status = "success"

            if self._bus is not None:
                from kiln.events import EventType
                self._bus.publish(
                    EventType.SYNC_COMPLETED,
                    data=result,
                    source="cloud_sync",
                )

        except Exception as exc:
            error_type = type(exc).__name__
            # Truncate and sanitize - don't expose full exception details
            raw_msg = str(exc)[:300]
            # Redact anything that looks like a URL with credentials
            import re
            safe_msg = re.sub(r'https?://[^@\s]*@', 'https://[CREDENTIALS]@', raw_msg)
            safe_msg = f"{error_type}: {safe_msg}"
            with self._lock:
                self._last_status = f"error: {safe_msg}"
                self._errors.append(safe_msg)
                if len(self._errors) > 50:
                    self._errors = self._errors[-50:]

            if self._bus is not None:
                from kiln.events import EventType
                self._bus.publish(
                    EventType.SYNC_FAILED,
                    data={"error": safe_msg},
                    source="cloud_sync",
                )

        return result

    def _push(self, entity_type: str, records: List[Dict[str, Any]]) -> None:
        """Push records to the cloud endpoint."""
        url = f"{self._config.cloud_url.rstrip('/')}/api/sync"
        payload = json.dumps({
            "type": entity_type,
            "records": records,
            "timestamp": time.time(),
        }).encode()

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._config.api_key}",
        }

        # Add HMAC signature
        sig = _compute_signature(self._config.api_key, payload)
        headers["X-Kiln-Signature"] = f"sha256={sig}"

        response = self._session.post(
            url, data=payload, headers=headers, timeout=30,
        )
        response.raise_for_status()
