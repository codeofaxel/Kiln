"""A print Kiln starts is ONE row that knows its version and its machine.

Before this, ``print_outcomes`` said a file printed on a printer label.
Every reader that learns per version or per machine (A/B, regression,
best-version, the fleet rollup) had to be fed by a separate hand-called
tool with its own table, and was not.  These tests pin the row:

- the identity columns exist on a fresh database AND are added to an
  old one by the migration;
- the start stamps what it knows (caller-named version, registry
  fingerprint, declared model, the nozzle on record) and nothing it
  does not (a foreign file has no version; an unidentifiable adapter
  has no fingerprint; a default nozzle is not a reading);
- an ending resolves the row in place, keeps the start's identity,
  stamps ``ended_at``, and never lets a weaker source relabel a
  stronger verdict.

A/B: every test here fails on the tree before the columns existed.
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any

import pytest


@pytest.fixture
def tmp_kiln_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setenv("HOME", str(tmp_path))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
    import kiln.persistence as _p

    monkeypatch.setattr(_p, "_db", None, raising=False)
    pro_hook_was_installed = False
    try:
        from kiln_pro.print_learning import auto_record as _pro_auto_record

        pro_hook_was_installed = _pro_auto_record.uninstall_auto_record_hook()
    except ImportError:
        pass
    yield tmp_path
    if pro_hook_was_installed:
        _pro_auto_record.install_auto_record_hook()
    monkeypatch.setattr(_p, "_db", None, raising=False)


IDENTITY = (
    "version_id",
    "design_id",
    "branch_id",
    "machine_fingerprint",
    "printer_model",
    "nozzle_diameter_mm",
    "nozzle_material",
    "ended_at",
    "account_sync_id",
)


def _columns(db_path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(print_outcomes)")}
    finally:
        conn.close()


class TestSchema:
    def test_fresh_database_carries_identity(self, tmp_kiln_env):
        from kiln.persistence import get_db

        get_db()
        assert set(IDENTITY) <= _columns(tmp_kiln_env / "kiln.db")

    def test_migration_adds_identity_to_an_old_table(self, tmp_path, monkeypatch):
        """An install from before the columns existed gets them on open."""
        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """CREATE TABLE print_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                printer_name TEXT NOT NULL, file_name TEXT, file_hash TEXT,
                material_type TEXT, outcome TEXT NOT NULL, quality_grade TEXT,
                failure_mode TEXT, settings TEXT, environment TEXT, notes TEXT,
                agent_id TEXT, created_at REAL NOT NULL)"""
        )
        conn.execute(
            "INSERT INTO print_outcomes (job_id, printer_name, outcome, created_at) "
            "VALUES ('old-1', 'p', 'success', 1.0)"
        )
        conn.commit()
        conn.close()
        assert not set(IDENTITY) & _columns(db_path)

        monkeypatch.setenv("KILN_DB_PATH", str(db_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        import kiln.persistence as _p

        monkeypatch.setattr(_p, "_db", None, raising=False)
        db = _p.get_db()
        assert set(IDENTITY) <= _columns(db_path)
        old = db.get_print_outcome("old-1")
        assert old is not None and old["version_id"] is None
        monkeypatch.setattr(_p, "_db", None, raising=False)

    def test_rank_and_vocabulary_agree(self):
        from kiln.persistence import KilnDB

        assert set(KilnDB.DETERMINED_BY_RANK) == KilnDB.VALID_DETERMINED_BY
        assert "seen" in KilnDB.VALID_DETERMINED_BY
        assert tuple(KilnDB.PRINT_OUTCOME_IDENTITY_COLUMNS) == IDENTITY


def _fake_adapter(*, serial: str | None = None, host: str | None = None, model: str = ""):
    from kiln.printers.base import PrinterAdapter, PrintResult

    class _Fake(PrinterAdapter):
        name = "fake-printer"

        def __init__(self):
            if serial:
                self.serial = serial
            if host:
                self.host = host

        def declared_printer_model(self) -> str:
            return model

        def _start_print_impl(self, file_name: str, **kwargs: Any) -> PrintResult:
            return PrintResult(success=True, message="started")

        def connect(self): ...
        def disconnect(self): ...
        def get_status(self): ...
        def get_printer_info(self): ...
        def list_files(self): ...
        def upload_file(self, file_path): ...
        def cancel_print(self): ...
        def pause_print(self): ...
        def resume_print(self): ...

    _Fake.__abstractmethods__ = frozenset()
    return _Fake()


class TestStartStamps:
    def test_a_design_print_lands_on_its_version(self, tmp_kiln_env, monkeypatch):
        from kiln import assumed_nozzle as _an
        from kiln.persistence import get_db

        monkeypatch.setattr(
            _an, "assumed_nozzle",
            lambda pid=None, **kw: _an.AssumedNozzle(0.6, "record", pid),
        )
        adapter = _fake_adapter(serial="01P00A1234", model="Bambu Lab A1")
        result = adapter.start_print(
            "bracket.gcode", version_id="v-abc", design_id="d-1", branch_id="b-main"
        )
        assert result.success
        rows = get_db().list_unresolved_outcomes(printer_name="fake-printer")
        assert len(rows) == 1
        row = rows[0]
        assert row["version_id"] == "v-abc"
        assert row["design_id"] == "d-1"
        assert row["branch_id"] == "b-main"
        assert row["machine_fingerprint"] == "fake-printer:serial:01p00a1234"
        assert row["printer_model"] == "Bambu Lab A1"
        assert row["nozzle_diameter_mm"] == 0.6
        assert row["ended_at"] is None

    def test_a_foreign_file_has_no_version_and_a_default_nozzle_is_not_a_reading(
        self, tmp_kiln_env, monkeypatch
    ):
        from kiln import assumed_nozzle as _an
        from kiln.persistence import get_db

        monkeypatch.setattr(
            _an, "assumed_nozzle",
            lambda pid=None, **kw: _an.AssumedNozzle(0.4, "default", pid),
        )
        adapter = _fake_adapter(host="http://192.168.1.20/")
        adapter.start_print("download.gcode")
        row = get_db().list_unresolved_outcomes(printer_name="fake-printer")[0]
        assert row["version_id"] is None and row["design_id"] is None
        assert row["machine_fingerprint"] == "fake-printer:host:192.168.1.20"
        assert row["nozzle_diameter_mm"] is None

    def test_an_unidentifiable_adapter_gets_no_fingerprint(self, tmp_kiln_env):
        from kiln.persistence import get_db

        _fake_adapter().start_print("x.gcode")
        row = get_db().list_unresolved_outcomes(printer_name="fake-printer")[0]
        assert row["machine_fingerprint"] is None

    def test_identity_never_raises_into_a_print(self, tmp_kiln_env, monkeypatch):
        import kiln.registry as _reg

        def _boom(adapter):
            raise RuntimeError("registry down")

        monkeypatch.setattr(_reg, "machine_fingerprint", _boom)
        from kiln.persistence import get_db

        assert _fake_adapter(serial="s").start_print("x.gcode", version_id="v-2").success
        row = get_db().list_unresolved_outcomes(printer_name="fake-printer")[0]
        # The rest of the identity still lands when one piece fails.
        assert row["version_id"] == "v-2" and row["machine_fingerprint"] is None


class TestEndingResolvesInPlace:
    def test_ending_keeps_identity_and_stamps_ended_at(self, tmp_kiln_env, monkeypatch):
        from kiln import assumed_nozzle as _an
        from kiln.persistence import get_db

        monkeypatch.setattr(
            _an, "assumed_nozzle",
            lambda pid=None, **kw: _an.AssumedNozzle(0.4, "printer_setting", pid),
        )
        adapter = _fake_adapter(serial="s1", model="M")
        before = time.time()
        adapter.start_print("bracket.gcode", version_id="v-1")
        db = get_db()
        db.save_print_outcome(
            {
                "job_id": "bracket",
                "printer_name": "fake-printer",
                "file_name": "bracket.gcode",
                "outcome": "success",
                "agent_id": "auto",
                "determined_by": "observed",
            }
        )
        rows = db.list_print_outcomes(printer_name="fake-printer")
        assert len(rows) == 1, "the ending resolved the start's row, not a second one"
        row = rows[0]
        assert row["outcome"] == "success"
        assert row["version_id"] == "v-1"
        assert row["machine_fingerprint"] == "fake-printer:serial:s1"
        assert row["nozzle_diameter_mm"] == 0.4
        assert row["ended_at"] is not None and row["ended_at"] >= before
        assert row["created_at"] <= row["ended_at"]

    def test_a_weaker_source_never_relabels_a_stronger_verdict(self, tmp_kiln_env):
        from kiln.persistence import get_db

        db = get_db()
        db.save_print_outcome(
            {"job_id": "j", "printer_name": "p", "outcome": "pending", "agent_id": "auto"}
        )
        db.save_print_outcome(
            {"job_id": "j", "printer_name": "p", "outcome": "failed",
             "agent_id": "mcp", "determined_by": "user_reported"}
        )
        db.save_print_outcome(
            {"job_id": "j", "printer_name": "p", "outcome": "failed",
             "agent_id": "mcp", "determined_by": "inferred"}
        )
        row = db.get_print_outcome("j")
        assert row["determined_by"] == "user_reported"

    def test_seen_is_a_valid_source_and_outranks_inferred(self, tmp_kiln_env):
        from kiln.persistence import get_db

        db = get_db()
        db.save_print_outcome(
            {"job_id": "j", "printer_name": "p", "outcome": "unknown",
             "agent_id": "auto", "determined_by": "inferred"}
        )
        db.save_print_outcome(
            {"job_id": "j", "printer_name": "p", "outcome": "success",
             "agent_id": "mcp", "determined_by": "seen"}
        )
        assert db.get_print_outcome("j")["determined_by"] == "seen"

    def test_record_tool_carries_the_ids_for_a_print_kiln_did_not_start(
        self, tmp_kiln_env, monkeypatch
    ):
        """The one record door accepts version/design/branch and lands them."""
        import kiln.server as _srv
        from kiln.persistence import get_db
        from kiln.plugins import learning_tools as lt

        monkeypatch.setattr(_srv, "_check_auth", lambda *_a, **_k: None)
        out = lt.record_print_outcome(
            job_id="foreign-job",
            outcome="success",
            printer_name="p",
            version_id="v-9",
            design_id="d-9",
        )
        assert out.get("success") or "error" not in out, out
        row = get_db().get_print_outcome("foreign-job")
        assert row["version_id"] == "v-9" and row["design_id"] == "d-9"
        assert row["determined_by"] == "user_reported"
