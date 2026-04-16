"""Tests for the live printer-model resolver.

Single source of truth: ``printer_model`` field on the active
printer's entry in ``~/.kiln/config.yaml``.  No inference, no env
var fallback, no source tagging — just read the file.  If the field
is absent, return ``None`` and let the safety stack soft-pass while
emitting a loud warning.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from kiln.printer_model_resolver import (
    invalidate_cache,
    resolve_printer_model,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_cache()
    yield
    invalidate_cache()


def _write_yaml(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


class TestExplicitPrinterModel:
    def test_reads_entry_field(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, """\
active_printer: default
printers:
  default:
    type: bambu
    host: 10.0.0.5
    serial: 03900D5C_test
    printer_model: bambu_a1
""")
        monkeypatch.setattr("kiln.printer_model_resolver._CONFIG_PATH", cfg)
        assert resolve_printer_model() == "bambu_a1"

    def test_reads_top_level_field(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, """\
active_printer: default
printer_model: prusa_mk4
printers:
  default:
    type: prusa
    host: prusa.local
""")
        monkeypatch.setattr("kiln.printer_model_resolver._CONFIG_PATH", cfg)
        assert resolve_printer_model() == "prusa_mk4"

    def test_honours_active_printer(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, """\
active_printer: printer_b
printers:
  printer_a:
    type: bambu
    printer_model: bambu_a1
  printer_b:
    type: bambu
    printer_model: bambu_x1c
""")
        monkeypatch.setattr("kiln.printer_model_resolver._CONFIG_PATH", cfg)
        assert resolve_printer_model() == "bambu_x1c"


class TestNoInference:
    """The resolver must NOT infer anything — no serial prefix, no host
    pattern, no env var, no per-type fallback.  When printer_model is
    missing, return None."""

    def test_bambu_serial_no_inference(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, """\
active_printer: default
printers:
  default:
    type: bambu
    host: 192.168.1.6
    serial: 03900D5C2513213
""")
        monkeypatch.setattr("kiln.printer_model_resolver._CONFIG_PATH", cfg)
        # No printer_model field → None, even though serial prefix WOULD
        # have mapped to bambu_a1 under the old inference logic.  We
        # intentionally scrapped that.
        assert resolve_printer_model() is None

    def test_env_var_ignored(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, """\
active_printer: default
printers:
  default:
    type: bambu
""")
        monkeypatch.setattr("kiln.printer_model_resolver._CONFIG_PATH", cfg)
        monkeypatch.setenv("KILN_PRINTER_MODEL", "ignored_model")
        assert resolve_printer_model() is None


class TestMissingConfig:
    def test_no_config_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiln.printer_model_resolver._CONFIG_PATH",
            tmp_path / "nonexistent.yaml",
        )
        assert resolve_printer_model() is None

    def test_empty_config_returns_none(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, "")
        monkeypatch.setattr("kiln.printer_model_resolver._CONFIG_PATH", cfg)
        assert resolve_printer_model() is None


class TestMalformedConfig:
    def test_bad_yaml_returns_none_not_raise(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, "{{{ not valid yaml }}}")
        monkeypatch.setattr("kiln.printer_model_resolver._CONFIG_PATH", cfg)
        # Safety-stack code must NEVER raise because the config is broken
        assert resolve_printer_model() is None


class TestCache:
    def test_mtime_change_invalidates_cache(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, """\
active_printer: default
printers:
  default:
    type: bambu
    printer_model: bambu_a1
""")
        monkeypatch.setattr("kiln.printer_model_resolver._CONFIG_PATH", cfg)
        assert resolve_printer_model() == "bambu_a1"

        _write_yaml(cfg, """\
active_printer: default
printers:
  default:
    type: bambu
    printer_model: bambu_x1c
""")
        os.utime(cfg, (time.time() + 10, time.time() + 10))
        assert resolve_printer_model() == "bambu_x1c"


class TestTypoWarning:
    """When printer_model is set but doesn't match any known printer,
    the resolver still returns the value (safety gates will soft-pass
    because they can't look up the profile) but logs a loud warning
    so the agent + user see the typo."""

    def test_unknown_model_returns_value_but_logs(
        self, tmp_path, monkeypatch, caplog,
    ):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, """\
active_printer: default
printers:
  default:
    type: bambu
    printer_model: bambu_A1
""")
        monkeypatch.setattr("kiln.printer_model_resolver._CONFIG_PATH", cfg)
        import logging
        with caplog.at_level(logging.WARNING, logger="kiln.printer_model_resolver"):
            result = resolve_printer_model()
        assert result == "bambu_A1"  # raw value preserved
        assert any(
            "doesn't match any known printer" in r.message
            for r in caplog.records
        )
