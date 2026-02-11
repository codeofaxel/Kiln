"""Tests for config file permission handling in kiln.cli.config.

Covers:
- _write_config_file sets 0600 permissions
- _read_config_file warns on overly permissive files
- _check_file_permissions detects group/other access
"""

from __future__ import annotations

import logging
import stat
from pathlib import Path
from typing import Any, Dict

import pytest

from kiln.cli.config import (
    _check_file_permissions,
    _read_config_file,
    _write_config_file,
)


class TestWriteConfigFilePermissions:
    """Tests for _write_config_file permission setting."""

    def test_sets_0600_permissions(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        _write_config_file(path, {"printers": {}})
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "subdir" / "config.yaml"
        _write_config_file(path, {"test": True})
        assert path.is_file()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_overwrites_with_correct_permissions(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("old content")
        path.chmod(0o644)
        _write_config_file(path, {"new": "data"})
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600


class TestCheckFilePermissions:
    """Tests for _check_file_permissions warning behaviour."""

    def test_no_warning_for_0600(self, tmp_path: Path, caplog) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("test")
        path.chmod(0o600)
        with caplog.at_level(logging.WARNING, logger="kiln.cli.config"):
            _check_file_permissions(path)
        assert len(caplog.records) == 0

    def test_warns_for_group_readable(self, tmp_path: Path, caplog) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("test")
        path.chmod(0o640)
        with caplog.at_level(logging.WARNING, logger="kiln.cli.config"):
            _check_file_permissions(path)
        assert any("permissive" in r.message.lower() for r in caplog.records)

    def test_warns_for_world_readable(self, tmp_path: Path, caplog) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("test")
        path.chmod(0o644)
        with caplog.at_level(logging.WARNING, logger="kiln.cli.config"):
            _check_file_permissions(path)
        assert any("permissive" in r.message.lower() for r in caplog.records)

    def test_no_crash_on_missing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "nonexistent.yaml"
        _check_file_permissions(path)  # Should not raise


class TestReadConfigFilePermissionWarning:
    """Tests that _read_config_file triggers permission warnings."""

    def test_warns_on_permissive_file(self, tmp_path: Path, caplog) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("printers: {}")
        path.chmod(0o644)
        with caplog.at_level(logging.WARNING, logger="kiln.cli.config"):
            data = _read_config_file(path)
        assert isinstance(data, dict)
        assert any("permissive" in r.message.lower() for r in caplog.records)

    def test_no_warning_on_secure_file(self, tmp_path: Path, caplog) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("printers: {}")
        path.chmod(0o600)
        with caplog.at_level(logging.WARNING, logger="kiln.cli.config"):
            data = _read_config_file(path)
        assert isinstance(data, dict)
        assert len(caplog.records) == 0
