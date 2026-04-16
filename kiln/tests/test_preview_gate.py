"""Tests for the preview-confirmation gate.

Motivation: on 2026-04-15, a Bambu A1 crashed because the agent sent a
print to the printer without showing the user a preview first.  Incident
#0 would have been caught by eye — but no preview was ever rendered.
This gate enforces that no start_print proceeds without explicit
confirmation that a preview was generated and the user approved it.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from kiln.preview_gate import (
    PreviewGate,
    get_preview_gate,
    hash_file,
)


@pytest.fixture
def gate() -> PreviewGate:
    return PreviewGate()


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    f = tmp_path / "disc.gcode.3mf"
    f.write_bytes(b"PK\x03\x04 fake 3mf bytes " * 10)
    return f


class TestTokenLifecycle:
    def test_issue_returns_valid_token(self, gate, sample_file):
        t = gate.issue(str(sample_file), printer_id="bambu_a1")
        assert t.token.startswith("pg_")
        assert len(t.token) > 10
        assert t.file_hash != ""
        assert t.printer_id == "bambu_a1"

    def test_valid_token_validates_ok(self, gate, sample_file):
        t = gate.issue(str(sample_file), printer_id="bambu_a1")
        ok, reason = gate.validate(t.token, str(sample_file), "bambu_a1")
        assert ok
        assert reason is None

    def test_token_is_single_use(self, gate, sample_file):
        t = gate.issue(str(sample_file), printer_id="bambu_a1")
        ok1, _ = gate.validate(t.token, str(sample_file), "bambu_a1")
        assert ok1
        ok2, reason = gate.validate(t.token, str(sample_file), "bambu_a1")
        assert not ok2
        assert reason == "token_not_found_or_already_used"


class TestTokenRejections:
    def test_unknown_token_rejected(self, gate, sample_file):
        ok, reason = gate.validate("pg_fake", str(sample_file))
        assert not ok
        assert reason == "token_not_found_or_already_used"

    def test_bad_format_rejected(self, gate, sample_file):
        ok, reason = gate.validate("not_a_token", str(sample_file))
        assert not ok
        assert reason == "invalid_token_format"

    def test_empty_token_rejected(self, gate, sample_file):
        ok, reason = gate.validate("", str(sample_file))
        assert not ok

    def test_file_hash_mismatch_rejected(self, gate, sample_file, tmp_path):
        """A token for fileA must NOT authorise printing fileB."""
        t = gate.issue(str(sample_file))
        other = tmp_path / "different.gcode"
        other.write_text("totally different contents")
        ok, reason = gate.validate(t.token, str(other))
        assert not ok
        assert reason == "token_file_hash_mismatch"

    def test_printer_mismatch_rejected(self, gate, sample_file):
        t = gate.issue(str(sample_file), printer_id="bambu_a1")
        ok, reason = gate.validate(
            t.token, str(sample_file), printer_id="prusa_mini",
        )
        assert not ok
        assert reason == "token_printer_mismatch"

    def test_expired_token_rejected(self, gate, sample_file):
        t = gate.issue(str(sample_file), ttl_seconds=1)
        time.sleep(1.1)
        ok, reason = gate.validate(t.token, str(sample_file))
        assert not ok
        assert reason == "token_expired"


class TestValidateConsumePolicy:
    def test_validate_with_consume_false_leaves_token(self, gate, sample_file):
        t = gate.issue(str(sample_file))
        ok1, _ = gate.validate(t.token, str(sample_file), consume=False)
        assert ok1
        ok2, _ = gate.validate(t.token, str(sample_file), consume=False)
        assert ok2  # still valid


class TestFileHashing:
    def test_same_file_same_hash(self, sample_file):
        h1 = hash_file(str(sample_file))
        h2 = hash_file(str(sample_file))
        assert h1 == h2

    def test_different_files_different_hashes(self, tmp_path):
        a = tmp_path / "a.stl"
        a.write_bytes(b"A" * 100)
        b = tmp_path / "b.stl"
        b.write_bytes(b"B" * 100)
        assert hash_file(str(a)) != hash_file(str(b))

    def test_missing_file_returns_sentinel(self):
        h = hash_file("/nonexistent/path.stl")
        assert h.startswith("NO_FILE:")


class TestModuleSingleton:
    def test_get_preview_gate_is_singleton(self):
        g1 = get_preview_gate()
        g2 = get_preview_gate()
        assert g1 is g2


class TestConcurrentIssuance:
    """Tokens must be unique even under concurrent issuance."""

    def test_many_tokens_are_all_unique(self, gate, sample_file):
        tokens = set()
        for _ in range(100):
            t = gate.issue(str(sample_file))
            tokens.add(t.token)
        assert len(tokens) == 100  # no collisions
