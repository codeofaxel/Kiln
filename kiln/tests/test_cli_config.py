"""Tests for kiln.cli.config — configuration management."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from kiln.cli.config import (
    _normalize_host,
    get_config_path,
    list_printers,
    load_printer_config,
    remove_printer,
    save_printer,
    set_active_printer,
    validate_printer_config,
)


# ---------------------------------------------------------------------------
# _normalize_host
# ---------------------------------------------------------------------------


class TestNormalizeHost:
    def test_adds_scheme(self):
        assert _normalize_host("octopi.local") == "http://octopi.local"

    def test_preserves_https(self):
        assert _normalize_host("https://printer.example.com") == "https://printer.example.com"

    def test_strips_trailing_slash(self):
        assert _normalize_host("http://host/") == "http://host"

    def test_strips_whitespace(self):
        assert _normalize_host("  http://host  ") == "http://host"

    def test_empty_string(self):
        assert _normalize_host("") == ""

    # Bambu-specific: raw IP, no http:// prefix
    def test_bambu_no_scheme_added(self):
        assert _normalize_host("192.168.1.100", "bambu") == "192.168.1.100"

    def test_bambu_strips_accidental_http(self):
        assert _normalize_host("http://192.168.1.100", "bambu") == "192.168.1.100"

    def test_bambu_strips_accidental_https(self):
        assert _normalize_host("https://192.168.1.100", "bambu") == "192.168.1.100"

    def test_bambu_strips_whitespace(self):
        assert _normalize_host("  192.168.1.100  ", "bambu") == "192.168.1.100"

    def test_bambu_strips_trailing_slash(self):
        assert _normalize_host("192.168.1.100/", "bambu") == "192.168.1.100"


# ---------------------------------------------------------------------------
# get_config_path
# ---------------------------------------------------------------------------


class TestGetConfigPath:
    def test_returns_home_based_path(self):
        p = get_config_path()
        assert p == Path.home() / ".kiln" / "config.yaml"


# ---------------------------------------------------------------------------
# save_printer / load_printer_config
# ---------------------------------------------------------------------------


class TestSaveAndLoad:
    def test_save_and_load_octoprint(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer(
            "ender3",
            "octoprint",
            "http://octopi.local",
            api_key="abc123",
            config_path=cfg_path,
        )
        cfg = load_printer_config("ender3", config_path=cfg_path)
        assert cfg["type"] == "octoprint"
        assert cfg["host"] == "http://octopi.local"
        assert cfg["api_key"] == "abc123"

    def test_save_and_load_moonraker(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer(
            "voron",
            "moonraker",
            "voron.local:7125",
            config_path=cfg_path,
        )
        cfg = load_printer_config("voron", config_path=cfg_path)
        assert cfg["type"] == "moonraker"
        assert cfg["host"] == "http://voron.local:7125"

    def test_save_and_load_bambu(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer(
            "x1c",
            "bambu",
            "192.168.1.100",
            access_code="12345678",
            serial="01P00A000000001",
            config_path=cfg_path,
        )
        cfg = load_printer_config("x1c", config_path=cfg_path)
        assert cfg["type"] == "bambu"
        assert cfg["host"] == "192.168.1.100"  # Raw IP, no http:// prefix
        assert cfg["access_code"] == "12345678"
        assert cfg["serial"] == "01P00A000000001"

    def test_bambu_env_var_no_http_prefix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_PRINTER_HOST", "192.168.1.100")
        monkeypatch.setenv("KILN_PRINTER_TYPE", "bambu")
        monkeypatch.setenv("KILN_PRINTER_API_KEY", "12345678")
        monkeypatch.setenv("KILN_PRINTER_SERIAL", "01P00A000000001")
        cfg = load_printer_config(config_path=tmp_path / "config.yaml")
        assert cfg["host"] == "192.168.1.100"  # No http:// prefix
        assert cfg["type"] == "bambu"

    def test_sets_active_by_default(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("a", "octoprint", "http://a", api_key="k", config_path=cfg_path)
        raw = yaml.safe_load(cfg_path.read_text())
        assert raw["active_printer"] == "a"

    def test_auto_select_single_printer(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("only", "moonraker", "http://only", config_path=cfg_path)
        # Load without specifying name — should auto-select the only one
        cfg = load_printer_config(config_path=cfg_path)
        assert cfg["type"] == "moonraker"

    def test_multiple_printers_requires_active(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("a", "moonraker", "http://a", config_path=cfg_path)
        save_printer("b", "moonraker", "http://b", set_active=False, config_path=cfg_path)
        # Remove active_printer key
        raw = yaml.safe_load(cfg_path.read_text())
        raw.pop("active_printer", None)
        cfg_path.write_text(yaml.safe_dump(raw))
        with pytest.raises(ValueError, match="Multiple printers"):
            load_printer_config(config_path=cfg_path)

    def test_printer_not_found(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("a", "moonraker", "http://a", config_path=cfg_path)
        with pytest.raises(ValueError, match="not found"):
            load_printer_config("nonexistent", config_path=cfg_path)

    def test_no_printers_configured(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("{}")
        with pytest.raises(ValueError, match="No printers configured"):
            load_printer_config(config_path=cfg_path)

    def test_env_var_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_PRINTER_HOST", "http://env-host")
        monkeypatch.setenv("KILN_PRINTER_TYPE", "moonraker")
        cfg = load_printer_config(config_path=tmp_path / "config.yaml")
        assert cfg["host"] == "http://env-host"
        assert cfg["type"] == "moonraker"

    def test_default_settings_applied(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("p", "moonraker", "http://p", config_path=cfg_path)
        cfg = load_printer_config("p", config_path=cfg_path)
        assert cfg["timeout"] == 30
        assert cfg["retries"] == 3


# ---------------------------------------------------------------------------
# set_active_printer
# ---------------------------------------------------------------------------


class TestSetActive:
    def test_set_active(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("a", "moonraker", "http://a", config_path=cfg_path)
        save_printer("b", "moonraker", "http://b", config_path=cfg_path)
        set_active_printer("a", config_path=cfg_path)
        raw = yaml.safe_load(cfg_path.read_text())
        assert raw["active_printer"] == "a"

    def test_set_active_not_found(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("a", "moonraker", "http://a", config_path=cfg_path)
        with pytest.raises(ValueError, match="not found"):
            set_active_printer("nope", config_path=cfg_path)


# ---------------------------------------------------------------------------
# list_printers
# ---------------------------------------------------------------------------


class TestListPrinters:
    def test_list_empty(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("{}")
        result = list_printers(config_path=cfg_path)
        assert result == []

    def test_list_multiple(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("a", "octoprint", "http://a", api_key="k", config_path=cfg_path)
        save_printer("b", "moonraker", "http://b", config_path=cfg_path)
        result = list_printers(config_path=cfg_path)
        assert len(result) == 2
        names = {p["name"] for p in result}
        assert names == {"a", "b"}
        # b should be active (last saved with set_active=True)
        active = [p for p in result if p["active"]]
        assert len(active) == 1
        assert active[0]["name"] == "b"


# ---------------------------------------------------------------------------
# remove_printer
# ---------------------------------------------------------------------------


class TestRemovePrinter:
    def test_remove(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("a", "moonraker", "http://a", config_path=cfg_path)
        save_printer("b", "moonraker", "http://b", config_path=cfg_path)
        remove_printer("b", config_path=cfg_path)
        result = list_printers(config_path=cfg_path)
        assert len(result) == 1
        assert result[0]["name"] == "a"

    def test_remove_active_selects_next(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("a", "moonraker", "http://a", config_path=cfg_path)
        save_printer("b", "moonraker", "http://b", config_path=cfg_path)
        # b is active
        remove_printer("b", config_path=cfg_path)
        raw = yaml.safe_load(cfg_path.read_text())
        assert raw["active_printer"] == "a"

    def test_remove_not_found(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("{}")
        with pytest.raises(ValueError, match="not found"):
            remove_printer("nope", config_path=cfg_path)


# ---------------------------------------------------------------------------
# validate_printer_config
# ---------------------------------------------------------------------------


class TestValidate:
    def test_valid_octoprint(self):
        ok, err = validate_printer_config({"type": "octoprint", "host": "http://h", "api_key": "k"})
        assert ok is True
        assert err is None

    def test_octoprint_missing_api_key(self):
        ok, err = validate_printer_config({"type": "octoprint", "host": "http://h"})
        assert ok is False
        assert "api_key" in err

    def test_valid_moonraker(self):
        ok, err = validate_printer_config({"type": "moonraker", "host": "http://h"})
        assert ok is True

    def test_valid_bambu(self):
        ok, err = validate_printer_config({
            "type": "bambu", "host": "http://h",
            "access_code": "abc", "serial": "123",
        })
        assert ok is True

    def test_bambu_missing_access_code(self):
        ok, err = validate_printer_config({"type": "bambu", "host": "http://h", "serial": "123"})
        assert ok is False
        assert "access_code" in err

    def test_bambu_missing_serial(self):
        ok, err = validate_printer_config({"type": "bambu", "host": "http://h", "access_code": "abc"})
        assert ok is False
        assert "serial" in err

    def test_unknown_type(self):
        ok, err = validate_printer_config({"type": "unknown", "host": "http://h"})
        assert ok is False

    def test_missing_host(self):
        ok, err = validate_printer_config({"type": "moonraker", "host": ""})
        assert ok is False
        assert "host" in err
