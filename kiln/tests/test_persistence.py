"""Tests for kiln.persistence -- SQLite persistence layer.

Covers:
- DB creation and schema (tables exist)
- Job CRUD operations (save, get, list, upsert)
- Event logging and retrieval
- Printer CRUD (save, list, remove)
- Settings get/set
- Thread safety (concurrent writes)
- Custom DB path via constructor
- Module-level singleton (get_db)
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

import pytest

from kiln.persistence import KilnDB, get_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """Return a KilnDB backed by a temporary file."""
    db_path = str(tmp_path / "test_kiln.db")
    instance = KilnDB(db_path=db_path)
    yield instance
    instance.close()


def _make_job(**overrides) -> dict:
    """Return a minimal valid job dict with optional overrides."""
    defaults = {
        "id": "job-001",
        "file_name": "benchy.gcode",
        "printer_name": None,
        "status": "queued",
        "priority": 0,
        "submitted_by": "agent",
        "submitted_at": time.time(),
        "started_at": None,
        "completed_at": None,
        "error_message": None,
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# DB creation and schema
# ---------------------------------------------------------------------------

class TestSchemaCreation:
    """Tests for database and schema initialization."""

    def test_db_file_created(self, tmp_path):
        db_path = str(tmp_path / "schema_test.db")
        assert not os.path.exists(db_path)
        instance = KilnDB(db_path=db_path)
        assert os.path.exists(db_path)
        instance.close()

    def test_creates_parent_directory(self, tmp_path):
        nested = tmp_path / "deep" / "nested" / "dir"
        db_path = str(nested / "kiln.db")
        instance = KilnDB(db_path=db_path)
        assert os.path.exists(db_path)
        instance.close()

    def test_jobs_table_exists(self, db):
        row = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        assert row is not None

    def test_events_table_exists(self, db):
        row = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
        assert row is not None

    def test_printers_table_exists(self, db):
        row = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='printers'"
        ).fetchone()
        assert row is not None

    def test_settings_table_exists(self, db):
        row = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='settings'"
        ).fetchone()
        assert row is not None

    def test_idempotent_schema_creation(self, tmp_path):
        db_path = str(tmp_path / "idempotent.db")
        db1 = KilnDB(db_path=db_path)
        db1.set_setting("k", "v")
        db1.close()

        # Re-open the same file -- should not raise or lose data
        db2 = KilnDB(db_path=db_path)
        assert db2.get_setting("k") == "v"
        db2.close()

    def test_path_property(self, db, tmp_path):
        assert db.path == str(tmp_path / "test_kiln.db")


# ---------------------------------------------------------------------------
# Job CRUD
# ---------------------------------------------------------------------------

class TestJobOperations:
    """Tests for save_job, get_job, list_jobs."""

    def test_save_and_get_job(self, db):
        job = _make_job()
        db.save_job(job)
        result = db.get_job("job-001")
        assert result is not None
        assert result["id"] == "job-001"
        assert result["file_name"] == "benchy.gcode"
        assert result["status"] == "queued"

    def test_get_job_not_found(self, db):
        assert db.get_job("nonexistent") is None

    def test_save_job_upsert(self, db):
        job = _make_job(status="queued")
        db.save_job(job)
        assert db.get_job("job-001")["status"] == "queued"

        job["status"] = "printing"
        job["started_at"] = time.time()
        db.save_job(job)
        result = db.get_job("job-001")
        assert result["status"] == "printing"
        assert result["started_at"] is not None

    def test_save_job_all_fields(self, db):
        now = time.time()
        job = _make_job(
            id="full-job",
            file_name="cube.gcode",
            printer_name="voron",
            status="completed",
            priority=5,
            submitted_by="claude",
            submitted_at=now - 100,
            started_at=now - 50,
            completed_at=now,
            error_message=None,
        )
        db.save_job(job)
        result = db.get_job("full-job")
        assert result["printer_name"] == "voron"
        assert result["priority"] == 5
        assert result["submitted_by"] == "claude"
        assert result["completed_at"] is not None

    def test_save_job_with_error(self, db):
        job = _make_job(
            id="fail-job",
            status="failed",
            error_message="thermal runaway",
        )
        db.save_job(job)
        result = db.get_job("fail-job")
        assert result["error_message"] == "thermal runaway"

    def test_list_jobs_all(self, db):
        for i in range(5):
            db.save_job(_make_job(id=f"job-{i}"))
        jobs = db.list_jobs()
        assert len(jobs) == 5

    def test_list_jobs_filter_by_status(self, db):
        db.save_job(_make_job(id="j1", status="queued"))
        db.save_job(_make_job(id="j2", status="printing"))
        db.save_job(_make_job(id="j3", status="queued"))

        queued = db.list_jobs(status="queued")
        assert len(queued) == 2
        assert all(j["status"] == "queued" for j in queued)

        printing = db.list_jobs(status="printing")
        assert len(printing) == 1
        assert printing[0]["id"] == "j2"

    def test_list_jobs_limit(self, db):
        for i in range(10):
            db.save_job(_make_job(id=f"job-{i}"))
        jobs = db.list_jobs(limit=3)
        assert len(jobs) == 3

    def test_list_jobs_ordered_by_priority_then_submitted_at(self, db):
        now = time.time()
        db.save_job(_make_job(id="low-old", priority=0, submitted_at=now - 10))
        db.save_job(_make_job(id="high", priority=10, submitted_at=now))
        db.save_job(_make_job(id="low-new", priority=0, submitted_at=now + 1))

        jobs = db.list_jobs()
        assert jobs[0]["id"] == "high"
        assert jobs[1]["id"] == "low-old"
        assert jobs[2]["id"] == "low-new"

    def test_list_jobs_empty(self, db):
        assert db.list_jobs() == []

    def test_get_job_returns_dict(self, db):
        db.save_job(_make_job())
        result = db.get_job("job-001")
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Event logging and retrieval
# ---------------------------------------------------------------------------

class TestEventOperations:
    """Tests for log_event and recent_events."""

    def test_log_event_returns_row_id(self, db):
        row_id = db.log_event("job.queued", {"job_id": "abc"})
        assert isinstance(row_id, int)
        assert row_id >= 1

    def test_log_event_sequential_ids(self, db):
        id1 = db.log_event("job.queued", {"x": 1})
        id2 = db.log_event("job.started", {"x": 2})
        assert id2 > id1

    def test_log_event_stores_data_as_json(self, db):
        data = {"printer": "voron", "temp": 205.0, "nested": {"a": 1}}
        db.log_event("print.started", data, source="test")

        events = db.recent_events()
        assert len(events) == 1
        assert events[0]["data"] == data
        assert events[0]["data"]["nested"]["a"] == 1

    def test_log_event_with_source(self, db):
        db.log_event("printer.error", {"msg": "timeout"}, source="printer:voron")
        events = db.recent_events()
        assert events[0]["source"] == "printer:voron"

    def test_log_event_with_custom_timestamp(self, db):
        ts = 1700000000.0
        db.log_event("job.completed", {}, timestamp=ts)
        events = db.recent_events()
        assert events[0]["timestamp"] == ts

    def test_log_event_default_timestamp(self, db):
        before = time.time()
        db.log_event("job.queued", {})
        after = time.time()

        events = db.recent_events()
        assert before <= events[0]["timestamp"] <= after

    def test_recent_events_newest_first(self, db):
        db.log_event("first", {}, timestamp=100.0)
        db.log_event("second", {}, timestamp=200.0)
        db.log_event("third", {}, timestamp=300.0)

        events = db.recent_events()
        assert events[0]["event_type"] == "third"
        assert events[1]["event_type"] == "second"
        assert events[2]["event_type"] == "first"

    def test_recent_events_filter_by_type(self, db):
        db.log_event("job.queued", {"i": 1})
        db.log_event("print.started", {"i": 2})
        db.log_event("job.queued", {"i": 3})

        events = db.recent_events(event_type="job.queued")
        assert len(events) == 2
        assert all(e["event_type"] == "job.queued" for e in events)

    def test_recent_events_limit(self, db):
        for i in range(10):
            db.log_event("print.progress", {"pct": i * 10})

        events = db.recent_events(limit=3)
        assert len(events) == 3

    def test_recent_events_empty(self, db):
        assert db.recent_events() == []

    def test_recent_events_filter_and_limit(self, db):
        for _ in range(5):
            db.log_event("job.queued", {})
        for _ in range(5):
            db.log_event("print.progress", {})

        events = db.recent_events(event_type="job.queued", limit=2)
        assert len(events) == 2
        assert all(e["event_type"] == "job.queued" for e in events)


# ---------------------------------------------------------------------------
# Printer CRUD
# ---------------------------------------------------------------------------

class TestPrinterOperations:
    """Tests for save_printer, list_printers, remove_printer."""

    def test_save_and_list_printer(self, db):
        db.save_printer("voron", "octoprint", "http://voron.local", api_key="KEY123")
        printers = db.list_printers()
        assert len(printers) == 1
        assert printers[0]["name"] == "voron"
        assert printers[0]["printer_type"] == "octoprint"
        assert printers[0]["host"] == "http://voron.local"
        assert printers[0]["api_key"] == "KEY123"

    def test_save_printer_without_api_key(self, db):
        db.save_printer("ender", "moonraker", "http://ender.local")
        printers = db.list_printers()
        assert printers[0]["api_key"] is None

    def test_save_printer_sets_timestamps(self, db):
        before = time.time()
        db.save_printer("voron", "octoprint", "http://voron.local")
        after = time.time()

        printers = db.list_printers()
        assert before <= printers[0]["registered_at"] <= after
        assert before <= printers[0]["last_seen"] <= after

    def test_save_printer_upsert(self, db):
        db.save_printer("voron", "octoprint", "http://old.local")
        db.save_printer("voron", "moonraker", "http://new.local")

        printers = db.list_printers()
        assert len(printers) == 1
        assert printers[0]["printer_type"] == "moonraker"
        assert printers[0]["host"] == "http://new.local"

    def test_list_printers_sorted_by_name(self, db):
        db.save_printer("zebra", "octoprint", "http://z.local")
        db.save_printer("alpha", "moonraker", "http://a.local")
        db.save_printer("middle", "bambu", "http://m.local")

        printers = db.list_printers()
        names = [p["name"] for p in printers]
        assert names == ["alpha", "middle", "zebra"]

    def test_list_printers_empty(self, db):
        assert db.list_printers() == []

    def test_remove_printer_exists(self, db):
        db.save_printer("voron", "octoprint", "http://voron.local")
        assert db.remove_printer("voron") is True
        assert db.list_printers() == []

    def test_remove_printer_not_found(self, db):
        assert db.remove_printer("nonexistent") is False

    def test_remove_printer_only_removes_target(self, db):
        db.save_printer("keep", "octoprint", "http://keep.local")
        db.save_printer("remove", "octoprint", "http://remove.local")

        db.remove_printer("remove")
        printers = db.list_printers()
        assert len(printers) == 1
        assert printers[0]["name"] == "keep"

    def test_multiple_printers(self, db):
        for i in range(5):
            db.save_printer(f"printer-{i}", "octoprint", f"http://p{i}.local")
        assert len(db.list_printers()) == 5


# ---------------------------------------------------------------------------
# Settings get/set
# ---------------------------------------------------------------------------

class TestSettingsOperations:
    """Tests for get_setting and set_setting."""

    def test_set_and_get_setting(self, db):
        db.set_setting("theme", "dark")
        assert db.get_setting("theme") == "dark"

    def test_get_setting_default(self, db):
        assert db.get_setting("missing") is None
        assert db.get_setting("missing", "fallback") == "fallback"

    def test_set_setting_overwrite(self, db):
        db.set_setting("mode", "auto")
        db.set_setting("mode", "manual")
        assert db.get_setting("mode") == "manual"

    def test_multiple_settings(self, db):
        db.set_setting("a", "1")
        db.set_setting("b", "2")
        db.set_setting("c", "3")
        assert db.get_setting("a") == "1"
        assert db.get_setting("b") == "2"
        assert db.get_setting("c") == "3"

    def test_setting_value_with_special_characters(self, db):
        value = '{"nested": "json", "list": [1,2,3]}'
        db.set_setting("config", value)
        assert db.get_setting("config") == value

    def test_setting_empty_string_value(self, db):
        db.set_setting("empty", "")
        assert db.get_setting("empty") == ""

    def test_get_setting_default_none(self, db):
        result = db.get_setting("nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety:
    """Tests for concurrent read/write operations."""

    def test_concurrent_job_writes(self, db):
        errors: list[Exception] = []

        def write_jobs(prefix: str, count: int) -> None:
            for i in range(count):
                try:
                    db.save_job(_make_job(
                        id=f"{prefix}-{i}",
                        submitted_at=time.time(),
                    ))
                except Exception as exc:
                    errors.append(exc)

        threads = [
            threading.Thread(target=write_jobs, args=(f"t{t}", 20))
            for t in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        jobs = db.list_jobs(limit=200)
        assert len(jobs) == 100

    def test_concurrent_event_writes(self, db):
        errors: list[Exception] = []
        ids: list[int] = []
        lock = threading.Lock()

        def log_events(count: int) -> None:
            for i in range(count):
                try:
                    row_id = db.log_event(
                        "print.progress",
                        {"i": i, "thread": threading.current_thread().name},
                    )
                    with lock:
                        ids.append(row_id)
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=log_events, args=(20,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(ids) == 100
        # All row IDs should be unique
        assert len(set(ids)) == 100

    def test_concurrent_setting_writes(self, db):
        errors: list[Exception] = []

        def write_settings(prefix: str, count: int) -> None:
            for i in range(count):
                try:
                    db.set_setting(f"{prefix}-key-{i}", f"value-{i}")
                except Exception as exc:
                    errors.append(exc)

        threads = [
            threading.Thread(target=write_settings, args=(f"t{t}", 10))
            for t in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

    def test_concurrent_reads_and_writes(self, db):
        errors: list[Exception] = []

        def writer() -> None:
            for i in range(50):
                try:
                    db.save_job(_make_job(id=f"rw-{i}", submitted_at=time.time()))
                except Exception as exc:
                    errors.append(exc)

        def reader() -> None:
            for _ in range(50):
                try:
                    db.list_jobs()
                    db.get_job("rw-0")
                except Exception as exc:
                    errors.append(exc)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(errors) == 0


# ---------------------------------------------------------------------------
# Custom DB path
# ---------------------------------------------------------------------------

class TestCustomDBPath:
    """Tests for custom database path via constructor."""

    def test_custom_path_via_constructor(self, tmp_path):
        custom_path = str(tmp_path / "custom" / "my.db")
        instance = KilnDB(db_path=custom_path)
        assert instance.path == custom_path
        assert os.path.exists(custom_path)
        instance.close()

    def test_env_var_override(self, tmp_path, monkeypatch):
        env_path = str(tmp_path / "env_override.db")
        monkeypatch.setenv("KILN_DB_PATH", env_path)
        instance = KilnDB()
        assert instance.path == env_path
        assert os.path.exists(env_path)
        instance.close()

    def test_constructor_path_overrides_env(self, tmp_path, monkeypatch):
        env_path = str(tmp_path / "env.db")
        constructor_path = str(tmp_path / "constructor.db")
        monkeypatch.setenv("KILN_DB_PATH", env_path)
        instance = KilnDB(db_path=constructor_path)
        assert instance.path == constructor_path
        instance.close()

    def test_separate_instances_are_independent(self, tmp_path):
        db1_path = str(tmp_path / "db1.db")
        db2_path = str(tmp_path / "db2.db")

        db1 = KilnDB(db_path=db1_path)
        db2 = KilnDB(db_path=db2_path)

        db1.set_setting("key", "from_db1")
        db2.set_setting("key", "from_db2")

        assert db1.get_setting("key") == "from_db1"
        assert db2.get_setting("key") == "from_db2"

        db1.close()
        db2.close()


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

class TestGetDB:
    """Tests for the module-level get_db() singleton."""

    def test_get_db_returns_kiln_db(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "singleton.db")
        monkeypatch.setenv("KILN_DB_PATH", db_path)

        # Reset the module singleton
        import kiln.persistence as mod
        mod._db = None

        instance = mod.get_db()
        assert isinstance(instance, KilnDB)
        assert os.path.exists(db_path)

        # Cleanup
        instance.close()
        mod._db = None

    def test_get_db_returns_same_instance(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "singleton2.db")
        monkeypatch.setenv("KILN_DB_PATH", db_path)

        import kiln.persistence as mod
        mod._db = None

        first = mod.get_db()
        second = mod.get_db()
        assert first is second

        # Cleanup
        first.close()
        mod._db = None


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------

class TestClose:
    """Tests for the close method."""

    def test_close_prevents_further_queries(self, tmp_path):
        db_path = str(tmp_path / "close_test.db")
        instance = KilnDB(db_path=db_path)
        instance.set_setting("test", "value")
        instance.close()

        with pytest.raises(Exception):
            instance.get_setting("test")

    def test_data_persists_after_close(self, tmp_path):
        db_path = str(tmp_path / "persist.db")

        db1 = KilnDB(db_path=db_path)
        db1.set_setting("persist_key", "persist_value")
        db1.save_job(_make_job(id="persist-job"))
        db1.log_event("test.event", {"key": "val"})
        db1.save_printer("voron", "octoprint", "http://voron.local")
        db1.close()

        db2 = KilnDB(db_path=db_path)
        assert db2.get_setting("persist_key") == "persist_value"
        assert db2.get_job("persist-job") is not None
        assert len(db2.recent_events()) == 1
        assert len(db2.list_printers()) == 1
        db2.close()
