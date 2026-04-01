"""Tests for get_prints_dir() and get_project_prints_dir()."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiln.cli.config import get_prints_dir, get_project_prints_dir


class TestGetPrintsDir:
    """Tests for get_prints_dir()."""

    def test_returns_path(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KILN_PRINTS_DIR", raising=False)
        config = tmp_path / "config.yaml"
        config.write_text("")
        result = get_prints_dir(config_path=config)
        assert isinstance(result, Path)

    def test_respects_env_var(self, tmp_path, monkeypatch):
        custom_dir = tmp_path / "custom_prints"
        monkeypatch.setenv("KILN_PRINTS_DIR", str(custom_dir))
        result = get_prints_dir()
        assert result == custom_dir
        assert result.is_dir()

    def test_respects_config_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KILN_PRINTS_DIR", raising=False)
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        config = config_dir / "config.yaml"
        prints = tmp_path / "from_config"
        config.write_text(f"prints_dir: {prints}\n")
        result = get_prints_dir(config_path=config)
        assert result == prints
        assert result.is_dir()

    def test_default_when_nothing_set(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KILN_PRINTS_DIR", raising=False)
        config = tmp_path / "config.yaml"
        config.write_text("")
        result = get_prints_dir(config_path=config)
        assert result == Path.home() / "Kiln" / "prints"

    def test_auto_creates_directory(self, tmp_path, monkeypatch):
        custom_dir = tmp_path / "new" / "nested" / "prints"
        monkeypatch.setenv("KILN_PRINTS_DIR", str(custom_dir))
        assert not custom_dir.exists()
        result = get_prints_dir()
        assert result.is_dir()

    def test_env_var_takes_precedence_over_config(self, tmp_path, monkeypatch):
        env_dir = tmp_path / "env_prints"
        cfg_dir = tmp_path / "cfg_prints"
        monkeypatch.setenv("KILN_PRINTS_DIR", str(env_dir))
        config = tmp_path / "config.yaml"
        config.write_text(f"prints_dir: {cfg_dir}\n")
        result = get_prints_dir(config_path=config)
        assert result == env_dir


class TestGetProjectPrintsDir:
    """Tests for get_project_prints_dir()."""

    def test_creates_subdirectory_structure(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_PRINTS_DIR", str(tmp_path / "prints"))
        project_dir = get_project_prints_dir("my-vase")
        assert project_dir == tmp_path / "prints" / "my-vase"
        assert project_dir.is_dir()
        for subdir in ("stl", "gcode", "3mf", "previews"):
            assert (project_dir / subdir).is_dir()

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_PRINTS_DIR", str(tmp_path / "prints"))
        dir1 = get_project_prints_dir("widget")
        # Place a file in one subdir to verify it persists.
        (dir1 / "stl" / "test.stl").write_text("solid")
        dir2 = get_project_prints_dir("widget")
        assert dir1 == dir2
        assert (dir2 / "stl" / "test.stl").read_text() == "solid"
