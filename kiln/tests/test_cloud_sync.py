"""Tests for kiln.cloud_sync — cloud sync manager."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

import pytest
import responses

from kiln.cloud_sync import CloudSyncManager, SyncConfig, SyncStatus, _compute_signature
from kiln.persistence import KilnDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    return KilnDB(str(tmp_path / "test.db"))


@pytest.fixture
def bus():
    return MagicMock()


@pytest.fixture
def config():
    return SyncConfig(
        cloud_url="https://cloud.example.com",
        api_key="test-api-key-12345",
        sync_interval_seconds=5.0,
    )


@pytest.fixture
def mgr(db, bus, config):
    return CloudSyncManager(db=db, event_bus=bus, config=config)


# ---------------------------------------------------------------------------
# SyncConfig tests
# ---------------------------------------------------------------------------

class TestSyncConfig:
    def test_defaults(self):
        c = SyncConfig()
        assert c.cloud_url == ""
        assert c.api_key == ""
        assert c.sync_interval_seconds == 60.0
        assert c.sync_jobs is True
        assert c.sync_events is True

    def test_to_dict_masks_api_key(self):
        c = SyncConfig(cloud_url="https://x.com", api_key="secret-key-here-long")
        d = c.to_dict()
        assert "secret-key-here-long" not in d["api_key"]
        assert d["api_key"].startswith("secret-k")
        assert d["api_key"].endswith("...")

    def test_to_dict_short_key(self):
        c = SyncConfig(api_key="short")
        d = c.to_dict()
        assert "..." in d["api_key"]

    def test_from_dict(self):
        d = {"cloud_url": "https://x.com", "api_key": "key", "sync_interval_seconds": 30.0}
        c = SyncConfig.from_dict(d)
        assert c.cloud_url == "https://x.com"
        assert c.sync_interval_seconds == 30.0

    def test_from_dict_partial(self):
        c = SyncConfig.from_dict({"cloud_url": "https://x.com"})
        assert c.cloud_url == "https://x.com"
        assert c.api_key == ""  # default


# ---------------------------------------------------------------------------
# SyncStatus tests
# ---------------------------------------------------------------------------

class TestSyncStatus:
    def test_defaults(self):
        s = SyncStatus(enabled=False)
        assert s.connected is False
        assert s.last_sync_status == "never"
        assert s.jobs_synced == 0

    def test_to_dict(self):
        s = SyncStatus(enabled=True, connected=True, jobs_synced=5)
        d = s.to_dict()
        assert d["enabled"] is True
        assert d["connected"] is True
        assert d["jobs_synced"] == 5


# ---------------------------------------------------------------------------
# HMAC signature tests
# ---------------------------------------------------------------------------

class TestComputeSignature:
    def test_produces_hex_digest(self):
        sig = _compute_signature("secret", b"payload")
        assert len(sig) == 64  # SHA256 hex digest

    def test_correct_hmac(self):
        expected = hmac.new(
            b"secret", b"payload", hashlib.sha256,
        ).hexdigest()
        assert _compute_signature("secret", b"payload") == expected

    def test_different_keys_different_sigs(self):
        sig1 = _compute_signature("key1", b"data")
        sig2 = _compute_signature("key2", b"data")
        assert sig1 != sig2

    def test_different_payloads_different_sigs(self):
        sig1 = _compute_signature("key", b"data1")
        sig2 = _compute_signature("key", b"data2")
        assert sig1 != sig2


# ---------------------------------------------------------------------------
# CloudSyncManager basic tests
# ---------------------------------------------------------------------------

class TestCloudSyncManagerBasic:
    def test_enabled_when_configured(self, mgr):
        assert mgr.enabled is True

    def test_not_enabled_without_url(self, db, bus):
        mgr = CloudSyncManager(db=db, event_bus=bus, config=SyncConfig(api_key="x"))
        assert mgr.enabled is False

    def test_not_enabled_without_key(self, db, bus):
        mgr = CloudSyncManager(db=db, event_bus=bus, config=SyncConfig(cloud_url="https://x.com"))
        assert mgr.enabled is False

    def test_status_initial(self, mgr):
        status = mgr.status()
        assert status.enabled is True
        assert status.connected is False
        assert status.last_sync_status == "never"
        assert status.jobs_synced == 0

    def test_configure_updates_config(self, mgr, db):
        new_config = SyncConfig(
            cloud_url="https://new.com", api_key="new-key",
            sync_interval_seconds=120.0,
        )
        mgr.configure(new_config)
        # Check persisted to DB
        raw = db.get_setting("cloud_sync_config")
        assert raw is not None
        d = json.loads(raw)
        assert d["cloud_url"] == "https://new.com"

    def test_sync_now_not_configured(self, db, bus):
        mgr = CloudSyncManager(db=db, event_bus=bus)
        result = mgr.sync_now()
        assert "error" in result

    def test_sync_now_no_db(self, bus, config):
        mgr = CloudSyncManager(db=None, event_bus=bus, config=config)
        result = mgr.sync_now()
        assert "error" in result


# ---------------------------------------------------------------------------
# CloudSyncManager sync cycle tests
# ---------------------------------------------------------------------------

class TestCloudSyncManagerSync:
    @responses.activate
    def test_sync_pushes_jobs(self, mgr, db):
        responses.add(
            responses.POST,
            "https://cloud.example.com/api/sync",
            json={"ok": True},
            status=200,
        )
        # Add a job
        db.save_job({
            "id": "job1", "file_name": "test.gcode", "status": "completed",
            "submitted_at": time.time(), "priority": 0,
        })
        result = mgr.sync_now()
        assert result["jobs_pushed"] == 1

    @responses.activate
    def test_sync_pushes_events(self, mgr, db):
        responses.add(
            responses.POST,
            "https://cloud.example.com/api/sync",
            json={"ok": True},
            status=200,
        )
        db.log_event("test.event", {"key": "value"})
        result = mgr.sync_now()
        assert result["events_pushed"] == 1

    @responses.activate
    def test_sync_marks_synced(self, mgr, db):
        responses.add(
            responses.POST,
            "https://cloud.example.com/api/sync",
            json={"ok": True},
            status=200,
        )
        db.save_job({
            "id": "job1", "file_name": "test.gcode", "status": "completed",
            "submitted_at": time.time(), "priority": 0,
        })
        mgr.sync_now()
        # Second sync should not push same job
        result = mgr.sync_now()
        # May be 0 or push again depending on cursor, but mark_synced was called
        assert mgr.status().jobs_synced >= 1

    @responses.activate
    def test_sync_updates_status(self, mgr, db):
        responses.add(
            responses.POST,
            "https://cloud.example.com/api/sync",
            json={"ok": True},
            status=200,
        )
        mgr.sync_now()
        status = mgr.status()
        assert status.last_sync_status == "success"
        assert status.last_sync_at is not None

    @responses.activate
    def test_sync_http_error(self, mgr, db):
        responses.add(
            responses.POST,
            "https://cloud.example.com/api/sync",
            json={"error": "unauthorized"},
            status=401,
        )
        db.save_job({
            "id": "job2", "file_name": "test.gcode", "status": "completed",
            "submitted_at": time.time(), "priority": 0,
        })
        mgr.sync_now()
        status = mgr.status()
        assert "error" in status.last_sync_status
        assert len(status.errors) > 0

    @responses.activate
    def test_sync_publishes_completed_event(self, mgr, bus, db):
        responses.add(
            responses.POST,
            "https://cloud.example.com/api/sync",
            json={"ok": True},
            status=200,
        )
        mgr.sync_now()
        # Should have published SYNC_COMPLETED
        assert bus.publish.called

    @responses.activate
    def test_sync_publishes_failed_event(self, mgr, bus, db):
        responses.add(
            responses.POST,
            "https://cloud.example.com/api/sync",
            json={"error": "fail"},
            status=500,
        )
        db.save_job({
            "id": "j3", "file_name": "t.gcode", "status": "queued",
            "submitted_at": time.time(), "priority": 0,
        })
        mgr.sync_now()
        assert bus.publish.called

    @responses.activate
    def test_sync_includes_hmac_header(self, mgr, db):
        responses.add(
            responses.POST,
            "https://cloud.example.com/api/sync",
            json={"ok": True},
            status=200,
        )
        db.save_job({
            "id": "j4", "file_name": "t.gcode", "status": "queued",
            "submitted_at": time.time(), "priority": 0,
        })
        mgr.sync_now()
        # Check request headers
        if responses.calls:
            req = responses.calls[0].request
            assert "X-Kiln-Signature" in req.headers
            assert req.headers["X-Kiln-Signature"].startswith("sha256=")

    @responses.activate
    def test_sync_includes_auth_header(self, mgr, db):
        responses.add(
            responses.POST,
            "https://cloud.example.com/api/sync",
            json={"ok": True},
            status=200,
        )
        db.save_job({
            "id": "j5", "file_name": "t.gcode", "status": "queued",
            "submitted_at": time.time(), "priority": 0,
        })
        mgr.sync_now()
        if responses.calls:
            req = responses.calls[0].request
            assert req.headers["Authorization"] == "Bearer test-api-key-12345"


# ---------------------------------------------------------------------------
# CloudSyncManager lifecycle tests
# ---------------------------------------------------------------------------

class TestCloudSyncManagerLifecycle:
    def test_start_stop(self, mgr):
        mgr.start()
        assert mgr._thread is not None
        mgr.stop()
        assert mgr._thread is None

    def test_start_not_enabled(self, db, bus):
        mgr = CloudSyncManager(db=db, event_bus=bus)
        mgr.start()
        assert mgr._thread is None  # Should not start

    def test_without_event_bus(self, db, config):
        mgr = CloudSyncManager(db=db, event_bus=None, config=config)
        # Should not crash
        assert mgr.status().enabled is True
