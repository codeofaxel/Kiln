"""Behaviour of resolve_active_printer_name against real config files."""
import textwrap
import pytest
from kiln import printer_model_resolver as pmr


def _write(tmp_path, monkeypatch, body: str):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent(body))
    monkeypatch.setattr(pmr, "_CONFIG_PATH", cfg)
    pmr.invalidate_cache()
    return cfg


def test_returns_the_name_its_owner_chose(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, """
        active_printer: workshop-a1
        printers:
          workshop-a1:
            host: 192.168.1.50
            type: bambu
          basement-mk4:
            host: 192.168.1.51
            type: prusalink
    """)
    assert pmr.resolve_active_printer_name() == "workshop-a1"


def test_one_printer_needs_no_declared_choice(tmp_path, monkeypatch):
    # A single configured printer is unambiguous whether or not the config
    # bothers to say which is active.
    _write(tmp_path, monkeypatch, """
        printers:
          workshop-a1:
            host: 192.168.1.50
    """)
    assert pmr.resolve_active_printer_name() == "workshop-a1"


def test_several_printers_without_a_choice_is_unanswerable(tmp_path, monkeypatch):
    # Guessing which of several is active is the mistake this exists to avoid.
    _write(tmp_path, monkeypatch, """
        printers:
          workshop-a1: {host: 192.168.1.50}
          basement-mk4: {host: 192.168.1.51}
    """)
    assert pmr.resolve_active_printer_name() is None


def test_the_default_placeholder_is_not_a_name(tmp_path, monkeypatch):
    # "default" is what Kiln falls back to when nobody chose. Handing it back
    # as the name of someone's printer is worse than saying nothing.
    _write(tmp_path, monkeypatch, """
        active_printer: default
        printers:
          default: {host: 192.168.1.50}
    """)
    assert pmr.resolve_active_printer_name() is None


def test_a_choice_pointing_at_nothing_is_not_a_name(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, """
        active_printer: sold-last-year
        printers:
          workshop-a1: {host: 192.168.1.50}
    """)
    assert pmr.resolve_active_printer_name() is None


def test_no_config_and_malformed_config_resolve_to_nothing(tmp_path, monkeypatch):
    missing = tmp_path / "nope.yaml"
    monkeypatch.setattr(pmr, "_CONFIG_PATH", missing)
    pmr.invalidate_cache()
    assert pmr.resolve_active_printer_name() is None

    _write(tmp_path, monkeypatch, "printers: [this, is, a, list]\n")
    assert pmr.resolve_active_printer_name() is None
