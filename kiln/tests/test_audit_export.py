"""Tests for audit trail export in kiln.persistence.KilnDB.export_audit_trail().

Coverage areas:
- JSON and CSV output formats
- Time range filtering (start_time, end_time)
- Field filtering (tool_name, action, session_id)
- Combined filters
- Empty result set
- Column presence and ordering
"""

from __future__ import annotations

import csv
import io
import json
import time

import pytest

from kiln.persistence import KilnDB


@pytest.fixture()
def db(tmp_path):
    """Return a KilnDB backed by a temporary file with sample audit entries."""
    db_path = str(tmp_path / "test_audit.db")
    instance = KilnDB(db_path=db_path)
    yield instance
    instance.close()


def _insert_audit_entry(db, *, tool_name="start_print", action="execute",
                        safety_level="normal", agent_id="agent-1",
                        printer_name="ender3", session_id="sess-1",
                        timestamp=None, details=None):
    """Insert a row directly into safety_audit_log."""
    ts = timestamp or time.time()
    details_json = json.dumps(details or {})
    db._conn.execute(
        """
        INSERT INTO safety_audit_log
            (timestamp, tool_name, safety_level, action,
             agent_id, printer_name, details, session_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ts, tool_name, safety_level, action, agent_id, printer_name,
         details_json, session_id),
    )
    db._conn.commit()


class TestExportAuditTrailJSON:
    """Tests for JSON export format."""

    def test_empty_audit_log_returns_empty_json_array(self, db):
        result = db.export_audit_trail(format="json")
        assert json.loads(result) == []

    def test_single_entry_returns_json_array(self, db):
        _insert_audit_entry(db, tool_name="get_printer_status", action="read")
        result = db.export_audit_trail(format="json")
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["tool_name"] == "get_printer_status"
        assert data[0]["action"] == "read"

    def test_json_contains_expected_columns(self, db):
        _insert_audit_entry(db)
        result = db.export_audit_trail(format="json")
        row = json.loads(result)[0]
        for col in ("id", "timestamp", "tool_name", "safety_level",
                     "action", "agent_id", "printer_name", "details"):
            assert col in row, f"Missing column: {col}"


class TestExportAuditTrailCSV:
    """Tests for CSV export format."""

    def test_empty_audit_log_returns_empty_csv(self, db):
        result = db.export_audit_trail(format="csv")
        assert result.strip() == ""

    def test_csv_has_header_and_row(self, db):
        _insert_audit_entry(db, tool_name="cancel_print")
        result = db.export_audit_trail(format="csv")
        reader = csv.DictReader(io.StringIO(result))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "cancel_print"

    def test_csv_multiple_rows(self, db):
        _insert_audit_entry(db, tool_name="start_print", timestamp=100.0)
        _insert_audit_entry(db, tool_name="cancel_print", timestamp=200.0)
        result = db.export_audit_trail(format="csv")
        reader = csv.DictReader(io.StringIO(result))
        rows = list(reader)
        assert len(rows) == 2


class TestExportAuditTrailFilters:
    """Tests for filtering audit trail entries."""

    def test_filter_by_tool_name(self, db):
        _insert_audit_entry(db, tool_name="start_print")
        _insert_audit_entry(db, tool_name="cancel_print")
        result = db.export_audit_trail(tool_name="start_print")
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["tool_name"] == "start_print"

    def test_filter_by_action(self, db):
        _insert_audit_entry(db, action="execute")
        _insert_audit_entry(db, action="read")
        result = db.export_audit_trail(action="read")
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["action"] == "read"

    def test_filter_by_session_id(self, db):
        _insert_audit_entry(db, session_id="sess-A")
        _insert_audit_entry(db, session_id="sess-B")
        result = db.export_audit_trail(session_id="sess-A")
        data = json.loads(result)
        assert len(data) == 1

    def test_filter_by_start_time(self, db):
        _insert_audit_entry(db, timestamp=100.0)
        _insert_audit_entry(db, timestamp=200.0)
        _insert_audit_entry(db, timestamp=300.0)
        result = db.export_audit_trail(start_time=150.0)
        data = json.loads(result)
        assert len(data) == 2
        assert all(row["timestamp"] >= 150.0 for row in data)

    def test_filter_by_end_time(self, db):
        _insert_audit_entry(db, timestamp=100.0)
        _insert_audit_entry(db, timestamp=200.0)
        _insert_audit_entry(db, timestamp=300.0)
        result = db.export_audit_trail(end_time=250.0)
        data = json.loads(result)
        assert len(data) == 2
        assert all(row["timestamp"] <= 250.0 for row in data)

    def test_filter_by_time_range(self, db):
        _insert_audit_entry(db, timestamp=100.0)
        _insert_audit_entry(db, timestamp=200.0)
        _insert_audit_entry(db, timestamp=300.0)
        result = db.export_audit_trail(start_time=150.0, end_time=250.0)
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["timestamp"] == 200.0

    def test_combined_filters(self, db):
        _insert_audit_entry(db, tool_name="start_print", action="execute",
                            session_id="s1", timestamp=100.0)
        _insert_audit_entry(db, tool_name="start_print", action="execute",
                            session_id="s2", timestamp=200.0)
        _insert_audit_entry(db, tool_name="cancel_print", action="execute",
                            session_id="s1", timestamp=300.0)
        result = db.export_audit_trail(
            tool_name="start_print", session_id="s1",
        )
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["timestamp"] == 100.0

    def test_no_matches_returns_empty(self, db):
        _insert_audit_entry(db, tool_name="start_print")
        result = db.export_audit_trail(tool_name="nonexistent_tool")
        assert json.loads(result) == []

    def test_results_ordered_by_timestamp_desc(self, db):
        _insert_audit_entry(db, timestamp=100.0)
        _insert_audit_entry(db, timestamp=300.0)
        _insert_audit_entry(db, timestamp=200.0)
        result = db.export_audit_trail()
        data = json.loads(result)
        timestamps = [row["timestamp"] for row in data]
        assert timestamps == sorted(timestamps, reverse=True)
