"""Tests for mDNS discovery whitelist (trusted printers).

Coverage:
- Config: get/add/remove/is_trusted
- Env var override
- Discovery annotation of trust status
- Edge cases: empty list, duplicate add, remove nonexistent
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from kiln.cli.config import (
    add_trusted_printer,
    get_trusted_printers,
    is_trusted_printer,
    remove_trusted_printer,
)
from kiln.discovery import DiscoveredPrinter, _annotate_trust


@pytest.fixture()
def config_path(tmp_path):
    """Return a temporary config file path."""
    return tmp_path / "config.yaml"


class TestGetTrustedPrinters:
    """get_trusted_printers() tests."""

    def test_empty_config_returns_empty_list(self, config_path):
        result = get_trusted_printers(config_path=config_path)
        assert result == []

    def test_returns_configured_printers(self, config_path):
        add_trusted_printer("192.168.1.100", config_path=config_path)
        add_trusted_printer("octopi.local", config_path=config_path)
        result = get_trusted_printers(config_path=config_path)
        assert "192.168.1.100" in result
        assert "octopi.local" in result

    def test_env_var_override(self, config_path):
        add_trusted_printer("192.168.1.100", config_path=config_path)
        with mock.patch.dict(os.environ, {"KILN_TRUSTED_PRINTERS": "10.0.0.1,10.0.0.2"}):
            result = get_trusted_printers(config_path=config_path)
        assert result == ["10.0.0.1", "10.0.0.2"]

    def test_env_var_strips_whitespace(self):
        with mock.patch.dict(os.environ, {"KILN_TRUSTED_PRINTERS": " 10.0.0.1 , 10.0.0.2 "}):
            result = get_trusted_printers()
        assert result == ["10.0.0.1", "10.0.0.2"]

    def test_env_var_ignores_empty_entries(self):
        with mock.patch.dict(os.environ, {"KILN_TRUSTED_PRINTERS": "10.0.0.1,,10.0.0.2,"}):
            result = get_trusted_printers()
        assert result == ["10.0.0.1", "10.0.0.2"]


class TestAddTrustedPrinter:
    """add_trusted_printer() tests."""

    def test_add_new_printer(self, config_path):
        add_trusted_printer("192.168.1.50", config_path=config_path)
        result = get_trusted_printers(config_path=config_path)
        assert "192.168.1.50" in result

    def test_add_duplicate_is_idempotent(self, config_path):
        add_trusted_printer("192.168.1.50", config_path=config_path)
        add_trusted_printer("192.168.1.50", config_path=config_path)
        result = get_trusted_printers(config_path=config_path)
        assert result.count("192.168.1.50") == 1

    def test_add_empty_host_raises(self, config_path):
        with pytest.raises(ValueError, match="host is required"):
            add_trusted_printer("", config_path=config_path)

    def test_add_whitespace_only_raises(self, config_path):
        with pytest.raises(ValueError, match="host is required"):
            add_trusted_printer("   ", config_path=config_path)


class TestRemoveTrustedPrinter:
    """remove_trusted_printer() tests."""

    def test_remove_existing(self, config_path):
        add_trusted_printer("192.168.1.50", config_path=config_path)
        remove_trusted_printer("192.168.1.50", config_path=config_path)
        result = get_trusted_printers(config_path=config_path)
        assert "192.168.1.50" not in result

    def test_remove_nonexistent_raises(self, config_path):
        with pytest.raises(ValueError, match="not in the trusted list"):
            remove_trusted_printer("10.0.0.99", config_path=config_path)


class TestIsTrustedPrinter:
    """is_trusted_printer() tests."""

    def test_trusted_returns_true(self, config_path):
        add_trusted_printer("192.168.1.50", config_path=config_path)
        assert is_trusted_printer("192.168.1.50", config_path=config_path) is True

    def test_untrusted_returns_false(self, config_path):
        assert is_trusted_printer("10.0.0.99", config_path=config_path) is False


class TestAnnotateTrust:
    """_annotate_trust() integration with DiscoveredPrinter."""

    def test_trusted_printer_is_flagged(self, config_path):
        printers = [
            DiscoveredPrinter(host="192.168.1.50", port=80, printer_type="octoprint"),
            DiscoveredPrinter(host="192.168.1.99", port=80, printer_type="moonraker"),
        ]
        with mock.patch("kiln.cli.config.get_trusted_printers", return_value=["192.168.1.50"]):
            _annotate_trust(printers)
        assert printers[0].trusted is True
        assert printers[1].trusted is False

    def test_empty_trusted_list(self):
        printers = [
            DiscoveredPrinter(host="192.168.1.50", port=80, printer_type="octoprint"),
        ]
        with mock.patch("kiln.cli.config.get_trusted_printers", return_value=[]):
            _annotate_trust(printers)
        assert printers[0].trusted is False

    def test_annotation_includes_trusted_in_to_dict(self, config_path):
        p = DiscoveredPrinter(host="192.168.1.50", port=80, printer_type="octoprint", trusted=True)
        d = p.to_dict()
        assert d["trusted"] is True

    def test_config_import_failure_is_handled(self):
        """If config import fails, printers should remain untrusted (no crash)."""
        printers = [
            DiscoveredPrinter(host="192.168.1.50", port=80, printer_type="octoprint"),
        ]
        with mock.patch("kiln.cli.config.get_trusted_printers", side_effect=ImportError("no config")):
            _annotate_trust(printers)
        assert printers[0].trusted is False
