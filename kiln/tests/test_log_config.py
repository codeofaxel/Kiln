"""Tests for kiln.log_config — log rotation and scrubbing."""

from __future__ import annotations

import logging
import os

import pytest

from kiln.log_config import ScrubFilter, configure_logging, _scrub


class TestScrubFilter:
    """Tests for the ScrubFilter logging filter."""

    def test_redacts_api_key_equals(self):
        result = _scrub("api_key=sk_live_abc123 foo")
        assert "sk_live_abc123" not in result
        assert "***REDACTED***" in result

    def test_redacts_api_key_colon(self):
        result = _scrub("api_key: my-secret-key")
        assert "my-secret-key" not in result
        assert "***REDACTED***" in result

    def test_redacts_bearer_token(self):
        result = _scrub("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload")
        assert "eyJhbGciOiJIUzI1NiJ9.payload" not in result
        assert "***REDACTED***" in result

    def test_redacts_basic_auth(self):
        result = _scrub("Authorization: Basic dXNlcjpwYXNz")
        assert "dXNlcjpwYXNz" not in result
        assert "***REDACTED***" in result

    def test_redacts_password(self):
        result = _scrub("password=hunter2")
        assert "hunter2" not in result
        assert "***REDACTED***" in result

    def test_redacts_access_code(self):
        result = _scrub("access_code=12345678")
        assert "12345678" not in result
        assert "***REDACTED***" in result

    def test_redacts_token(self):
        result = _scrub("token=tok_live_xyz")
        assert "tok_live_xyz" not in result
        assert "***REDACTED***" in result

    def test_redacts_secret(self):
        result = _scrub("secret=whsec_abc123")
        assert "whsec_abc123" not in result
        assert "***REDACTED***" in result

    def test_preserves_non_sensitive(self):
        msg = "Printer status: idle, temperature: 200C"
        assert _scrub(msg) == msg

    def test_filter_modifies_log_record(self):
        f = ScrubFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="api_key=secret123", args=(), exc_info=None,
        )
        f.filter(record)
        assert "secret123" not in record.msg
        assert "***REDACTED***" in record.msg

    def test_filter_scrubs_tuple_args(self):
        f = ScrubFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="connecting with %s", args=("api_key=secret123",),
            exc_info=None,
        )
        f.filter(record)
        assert "secret123" not in record.args[0]

    def test_filter_scrubs_dict_args(self):
        f = ScrubFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="%(key)s", args=None,
            exc_info=None,
        )
        record.args = {"key": "token=abc"}
        f.filter(record)
        assert "abc" not in record.args["key"]

    def test_filter_returns_true(self):
        f = ScrubFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello", args=(), exc_info=None,
        )
        assert f.filter(record) is True


class TestConfigureLogging:
    """Tests for configure_logging()."""

    def test_creates_log_directory(self, tmp_path):
        log_dir = str(tmp_path / "logs")
        configure_logging(log_dir)
        assert os.path.isdir(log_dir)
        # Clean up handlers to avoid side effects
        root = logging.getLogger()
        from logging.handlers import RotatingFileHandler
        root.handlers = [h for h in root.handlers if not isinstance(h, RotatingFileHandler)]

    def test_creates_rotating_file_handler(self, tmp_path):
        log_dir = str(tmp_path / "logs2")
        configure_logging(log_dir)
        root = logging.getLogger()
        from logging.handlers import RotatingFileHandler
        rotating = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        assert len(rotating) >= 1
        handler = rotating[-1]
        assert handler.maxBytes == 10_000_000
        assert handler.backupCount == 5
        # Clean up
        root.handlers = [h for h in root.handlers if not isinstance(h, RotatingFileHandler)]

    def test_installs_scrub_filter(self, tmp_path):
        log_dir = str(tmp_path / "logs3")
        configure_logging(log_dir)
        root = logging.getLogger()
        from logging.handlers import RotatingFileHandler
        rotating = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        assert any(isinstance(f, ScrubFilter) for f in rotating[-1].filters)
        # Clean up
        root.handlers = [h for h in root.handlers if not isinstance(h, RotatingFileHandler)]

    def test_respects_env_log_level(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_LOG_LEVEL", "DEBUG")
        log_dir = str(tmp_path / "logs4")
        configure_logging(log_dir)
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        # Clean up
        from logging.handlers import RotatingFileHandler
        root.handlers = [h for h in root.handlers if not isinstance(h, RotatingFileHandler)]
        root.setLevel(logging.WARNING)

    def test_custom_rotation_params(self, tmp_path):
        log_dir = str(tmp_path / "logs5")
        configure_logging(log_dir, max_bytes=5_000_000, backup_count=3)
        root = logging.getLogger()
        from logging.handlers import RotatingFileHandler
        rotating = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        handler = rotating[-1]
        assert handler.maxBytes == 5_000_000
        assert handler.backupCount == 3
        # Clean up
        root.handlers = [h for h in root.handlers if not isinstance(h, RotatingFileHandler)]
