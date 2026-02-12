"""Tests for HMAC-signed audit log entries in persistence.py."""

from __future__ import annotations

import hashlib
import hmac
import json
import os

import pytest

from kiln.persistence import KilnDB


class TestAuditHMAC:
    """Tests for HMAC signing and verification of audit log entries."""

    @pytest.fixture()
    def db(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        return KilnDB(db_path=db_path)

    def test_log_audit_stores_hmac(self, db):
        row_id = db.log_audit(
            tool_name="start_print",
            safety_level="guarded",
            action="executed",
            details={"file": "benchy.gcode"},
        )
        row = db._conn.execute(
            "SELECT hmac_signature FROM safety_audit_log WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row is not None
        assert row["hmac_signature"] is not None
        assert len(row["hmac_signature"]) == 64  # SHA256 hex digest

    def test_verify_audit_all_valid(self, db):
        db.log_audit(tool_name="t1", safety_level="safe", action="executed")
        db.log_audit(tool_name="t2", safety_level="guarded", action="blocked")
        result = db.verify_audit_log()
        assert result["total"] == 2
        assert result["valid"] == 2
        assert result["invalid"] == 0
        assert result["integrity"] == "ok"

    def test_verify_audit_detects_tampering(self, db):
        db.log_audit(tool_name="t1", safety_level="safe", action="executed")
        # Tamper with the action field
        db._conn.execute(
            "UPDATE safety_audit_log SET action = 'forged' WHERE id = 1"
        )
        db._conn.commit()
        result = db.verify_audit_log()
        assert result["total"] == 1
        assert result["invalid"] == 1
        assert result["valid"] == 0
        assert result["integrity"] == "compromised"

    def test_verify_audit_empty_log(self, db):
        result = db.verify_audit_log()
        assert result["total"] == 0
        assert result["valid"] == 0
        assert result["invalid"] == 0
        assert result["integrity"] == "ok"

    def test_hmac_uses_env_key(self, db, monkeypatch):
        monkeypatch.setenv("KILN_AUDIT_HMAC_KEY", "custom-secret-key")
        row_id = db.log_audit(
            tool_name="test", safety_level="safe", action="executed",
        )
        row = db._conn.execute(
            "SELECT hmac_signature FROM safety_audit_log WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["hmac_signature"] is not None
        # Verify it validates with the env key
        result = db.verify_audit_log()
        assert result["valid"] == 1

    def test_hmac_with_all_fields(self, db):
        row_id = db.log_audit(
            tool_name="send_gcode",
            safety_level="confirm",
            action="executed",
            agent_id="agent-1",
            printer_name="ender3",
            details={"commands": ["G28"]},
        )
        result = db.verify_audit_log()
        assert result["valid"] == 1

    def test_hmac_null_optional_fields(self, db):
        row_id = db.log_audit(
            tool_name="status",
            safety_level="safe",
            action="executed",
        )
        result = db.verify_audit_log()
        assert result["valid"] == 1
