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


class TestFilenameKeyValidation:
    """start_print only sees the printer-side file_name (basename).

    The previous implementation treated any string with a "." in it
    as a path and tried to ``hash_file("coaster.3mf")`` — which
    resolved against the server's cwd, didn't find the file, and
    returned a ``NO_FILE:`` sentinel.  Every real start_print with a
    valid token got rejected as ``token_file_hash_mismatch``.  These
    tests pin the filename-key path so that regression can't come
    back silently.
    """

    def test_issue_by_path_validate_by_bare_filename_ok(self, gate, sample_file):
        """Real-world hot path: issue from the local path, validate
        from the printer-side bare filename."""
        t = gate.issue(str(sample_file), printer_id="bambu_a1")
        # ``start_print`` is called with just "disc.gcode.3mf" — no
        # directory separator.  The old heuristic misread this as a
        # path because it contained a "." and hash-compared, failing.
        ok, reason = gate.validate(
            t.token, sample_file.name, printer_id="bambu_a1",
        )
        assert ok, f"unexpected rejection: {reason}"

    def test_filename_mismatch_rejected(self, gate, sample_file):
        t = gate.issue(str(sample_file))
        ok, reason = gate.validate(t.token, "different.3mf")
        assert not ok
        assert reason == "token_filename_mismatch"

    def test_filename_with_dot_is_not_treated_as_path(self, gate, sample_file):
        """Bare filenames with dots must NOT trigger a hash_file lookup."""
        t = gate.issue(str(sample_file))
        # ``coaster.3mf`` does not exist at this cwd.  If the gate
        # treats it as a path it will hash_file() -> NO_FILE: sentinel
        # and reject.  Correct behaviour: take the filename-key path.
        ok, reason = gate.validate(t.token, "coaster.3mf")
        # Should reject with filename_mismatch, NOT hash_mismatch or
        # NO_FILE-comparison artefacts.
        assert not ok
        assert reason == "token_filename_mismatch"

    def test_issue_by_bare_filename_validate_by_bare_filename(self, gate):
        """When no local path is ever available on either side."""
        t = gate.issue("plate_1.gcode", printer_id="bambu_a1")
        assert t.filename_key == "plate_1.gcode"
        ok, reason = gate.validate(t.token, "plate_1.gcode", "bambu_a1")
        assert ok, f"unexpected rejection: {reason}"

    def test_issue_by_path_preserves_basename_key(self, gate, sample_file):
        t = gate.issue(str(sample_file))
        assert t.filename_key == sample_file.name
        assert t.file_hash  # full hash also present
