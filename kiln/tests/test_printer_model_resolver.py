"""Tests for the live printer-model resolver.

The safety stack consumers call :func:`resolve_printer_model` instead
of reading a frozen module global.  This lets them see config.yaml
changes without a server restart, and gives them deterministic
fallbacks when the user hasn't set the ``printer_model`` field
explicitly.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kiln.printer_model_resolver import (
    _BAMBU_SERIAL_PREFIXES,
    _TYPE_FALLBACKS,
    invalidate_cache,
    resolve_printer_model,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure every test starts with a cold cache."""
    invalidate_cache()
    yield
    invalidate_cache()


def _write_yaml(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


class TestExplicitPrinterModelField:
    """``printer_model`` in config.yaml is always the highest-priority source."""

    def test_explicit_entry_field(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, """\
active_printer: default
printers:
  default:
    type: bambu
    host: 10.0.0.5
    serial: 03900D5C_test
    printer_model: bambu_x1c
""")
        monkeypatch.setattr(
            "kiln.printer_model_resolver._CONFIG_PATH", cfg,
        )
        assert resolve_printer_model() == "bambu_x1c"

    def test_explicit_top_level_field(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, """\
active_printer: default
printer_model: prusa_mk4
printers:
  default:
    type: prusa
    host: prusa.local
""")
        monkeypatch.setattr(
            "kiln.printer_model_resolver._CONFIG_PATH", cfg,
        )
        assert resolve_printer_model() == "prusa_mk4"


class TestBambuSerialInference:
    """When printer_model isn't explicit, Bambu serials deterministically
    resolve to a known model via prefix lookup."""

    def test_a1_serial_prefix(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, """\
active_printer: default
printers:
  default:
    type: bambu
    host: 192.168.1.6
    serial: 03900D5C2513213
""")
        monkeypatch.setattr(
            "kiln.printer_model_resolver._CONFIG_PATH", cfg,
        )
        assert resolve_printer_model() == "bambu_a1"

    def test_x1c_longer_prefix_wins(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, """\
active_printer: default
printers:
  default:
    type: bambu
    host: 192.168.1.6
    serial: 03919XX_test
""")
        monkeypatch.setattr(
            "kiln.printer_model_resolver._CONFIG_PATH", cfg,
        )
        # 03919 matches both "039" and "03919" — longer prefix wins
        assert resolve_printer_model() == "bambu_x1c"

    def test_unknown_bambu_serial_returns_none_not_guess(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, """\
active_printer: default
printers:
  default:
    type: bambu
    host: 192.168.1.6
    serial: XYZZZZ_future_model
""")
        monkeypatch.setattr(
            "kiln.printer_model_resolver._CONFIG_PATH", cfg,
        )
        # We refuse to guess when the prefix doesn't match — safer than
        # applying wrong limits to a new Bambu model we haven't profiled
        assert resolve_printer_model() is None


class TestNonBambuInference:
    def test_prusa_mk4_host_pattern(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, """\
active_printer: default
printers:
  default:
    type: prusa
    host: http://prusa-mk4.local
""")
        monkeypatch.setattr(
            "kiln.printer_model_resolver._CONFIG_PATH", cfg,
        )
        assert resolve_printer_model() == "prusa_mk4"

    def test_prusa_mini_host_pattern(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, """\
active_printer: default
printers:
  default:
    type: prusa
    host: http://printer-mini.local
""")
        monkeypatch.setattr(
            "kiln.printer_model_resolver._CONFIG_PATH", cfg,
        )
        assert resolve_printer_model() == "prusa_mini"

    def test_moonraker_falls_back_to_klipper_generic(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, """\
active_printer: default
printers:
  default:
    type: moonraker
    host: http://10.0.0.50
""")
        monkeypatch.setattr(
            "kiln.printer_model_resolver._CONFIG_PATH", cfg,
        )
        assert resolve_printer_model() == "klipper_generic"

    def test_octoprint_refuses_to_guess(self, tmp_path, monkeypatch):
        """OctoPrint can front any printer — guessing would be worse than None."""
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, """\
active_printer: default
printers:
  default:
    type: octoprint
    host: http://octopi.local
""")
        monkeypatch.setattr(
            "kiln.printer_model_resolver._CONFIG_PATH", cfg,
        )
        assert resolve_printer_model() is None


class TestEnvVarFallback:
    def test_env_var_used_when_yaml_cant_resolve(
        self, tmp_path, monkeypatch,
    ):
        # Empty yaml
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, "")
        monkeypatch.setattr(
            "kiln.printer_model_resolver._CONFIG_PATH", cfg,
        )
        monkeypatch.setenv("KILN_PRINTER_MODEL", "custom_unit_001")
        assert resolve_printer_model() == "custom_unit_001"


class TestNoConfigAndNoEnv:
    def test_returns_none_cleanly(self, tmp_path, monkeypatch):
        # Config doesn't exist
        cfg = tmp_path / "nonexistent.yaml"
        monkeypatch.setattr(
            "kiln.printer_model_resolver._CONFIG_PATH", cfg,
        )
        monkeypatch.delenv("KILN_PRINTER_MODEL", raising=False)
        assert resolve_printer_model() is None


class TestCacheInvalidation:
    def test_cache_reflects_yaml_mtime_changes(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, """\
active_printer: default
printers:
  default:
    type: bambu
    host: 10.0.0.5
    printer_model: bambu_a1
""")
        monkeypatch.setattr(
            "kiln.printer_model_resolver._CONFIG_PATH", cfg,
        )
        assert resolve_printer_model() == "bambu_a1"

        # Rewrite with a different model
        import os
        import time
        _write_yaml(cfg, """\
active_printer: default
printers:
  default:
    type: bambu
    host: 10.0.0.5
    printer_model: bambu_x1c
""")
        # Bump mtime so cache invalidates
        os.utime(cfg, (time.time() + 10, time.time() + 10))
        invalidate_cache()
        assert resolve_printer_model() == "bambu_x1c"


class TestMalformedConfig:
    def test_broken_yaml_returns_none_not_raise(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, "{{{ this is not valid yaml }}}")
        monkeypatch.setattr(
            "kiln.printer_model_resolver._CONFIG_PATH", cfg,
        )
        # Should NOT raise — safety-stack code must never fail because
        # the config is malformed
        assert resolve_printer_model() is None


class TestCoverageOfSupportedTypes:
    """Sanity: the fallbacks table covers the printer types Kiln
    adapters support."""

    def test_fallbacks_table_has_expected_keys(self):
        # Not exhaustive, but catches drift if adapters are added/removed
        assert "prusa" in _TYPE_FALLBACKS
        assert "moonraker" in _TYPE_FALLBACKS
        assert "octoprint" in _TYPE_FALLBACKS

    def test_bambu_prefixes_cover_flagship_models(self):
        assert any(m == "bambu_a1" for m in _BAMBU_SERIAL_PREFIXES.values())
        assert any(m == "bambu_x1c" for m in _BAMBU_SERIAL_PREFIXES.values())
        assert any(m == "bambu_a1_mini" for m in _BAMBU_SERIAL_PREFIXES.values())
