"""`prusaconnect` -> `prusalink` legacy-type migration (renamed in 1.1.5).

Saved configs that still pin the old `type: prusaconnect` must keep working
— auto-normalized on read — instead of failing with "unsupported printer
type".  Regression guard for the v1.1.5 adapter rename.
"""
import yaml

from kiln.cli.config import (
    _normalize_printer_type,
    load_printer_config,
    validate_printer_config,
)


def test_normalize_maps_prusaconnect_to_prusalink():
    assert _normalize_printer_type("prusaconnect") == "prusalink"


def test_normalize_passes_through_current_types():
    for t in ("octoprint", "moonraker", "creality", "bambu", "elegoo", "prusalink", "serial"):
        assert _normalize_printer_type(t) == t


def test_saved_config_with_legacy_type_loads_as_prusalink(tmp_path, monkeypatch):
    monkeypatch.delenv("KILN_PRINTER_HOST", raising=False)  # force the file path
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "active_printer": "prusa",
        "printers": {
            "prusa": {"type": "prusaconnect", "host": "http://192.168.1.44", "api_key": "K"},
        },
        "settings": {"timeout": 30, "retries": 3},
    }))
    cfg = load_printer_config("prusa", config_path=cfg_path)
    assert cfg["type"] == "prusalink"


def test_env_config_with_legacy_type_loads_as_prusalink(monkeypatch):
    monkeypatch.setenv("KILN_PRINTER_HOST", "http://192.168.1.44")
    monkeypatch.setenv("KILN_PRINTER_TYPE", "prusaconnect")
    cfg = load_printer_config()
    assert cfg["type"] == "prusalink"


def test_validate_accepts_legacy_prusaconnect():
    ok, err = validate_printer_config({"type": "prusaconnect", "host": "http://192.168.1.44"})
    assert ok, err
