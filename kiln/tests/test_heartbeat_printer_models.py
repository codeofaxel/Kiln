"""Heartbeat printer-model resolution.

Regression guard for a silent production blindness: the heartbeat
resolved the model via ``adapter.get_printer_info()`` — a method no
adapter has ever implemented — so every call raised into a bare except
and 630 of 670 production heartbeat rows carried a NULL model while
adapter_type resolved fine for the same rows.  The fix resolves the way
the registry's own fleet view does (the ``printer_model`` attribute)
plus config.yaml via printer_model_resolver.
"""
from __future__ import annotations

import textwrap

from kiln import heartbeat
from kiln import printer_model_resolver


class _AttrAdapter:
    """Adapter shaped like BambuAdapter: model in an attribute only."""

    def __init__(self, model: str | None):
        self._printer_model = model


class _InfoAdapter:
    """Adapter with a live get_printer_info() probe (none exist today,
    but the resolution order keeps it winning if one appears)."""

    class _Info:
        model = "probed_model"

    def get_printer_info(self):
        return self._Info()


class _Registry:
    def __init__(self, adapters: dict[str, object]):
        self._adapters = adapters
        self.count = len(adapters)

    def get(self, name):
        return self._adapters.get(name)

    def list_names(self):
        return list(self._adapters)


def _install_registry(monkeypatch, adapters: dict[str, object]):
    import kiln.registry as registry_mod

    monkeypatch.setattr(
        registry_mod, "get_registry", lambda: _Registry(adapters)
    )


def test_adapter_model_reads_attribute_like_the_registry_fleet_view():
    assert heartbeat._adapter_model(_AttrAdapter("bambu_a1")) == "bambu_a1"
    assert heartbeat._adapter_model(_AttrAdapter(None)) is None
    assert heartbeat._adapter_model(_AttrAdapter("  ")) is None


def test_adapter_model_prefers_live_probe_when_available():
    assert heartbeat._adapter_model(_InfoAdapter()) == "probed_model"


def test_get_printer_info_resolves_from_adapter_attribute(monkeypatch):
    _install_registry(monkeypatch, {"default": _AttrAdapter("bambu_a1")})
    monkeypatch.delenv("KILN_PRINTER_MODEL", raising=False)
    model, _adapter_type, count = heartbeat._get_printer_info()
    assert model == "bambu_a1"
    assert count == 1


def test_get_printer_info_falls_back_to_config_resolver(monkeypatch):
    _install_registry(monkeypatch, {"default": _AttrAdapter(None)})
    monkeypatch.delenv("KILN_PRINTER_MODEL", raising=False)
    monkeypatch.setattr(
        printer_model_resolver, "resolve_printer_model", lambda: "prusa_mini"
    )
    model, _adapter_type, _count = heartbeat._get_printer_info()
    assert model == "prusa_mini"


def test_all_models_union_of_adapters_and_config(monkeypatch, tmp_path):
    _install_registry(monkeypatch, {
        "default": _AttrAdapter("bambu_a1"),
        "second": _AttrAdapter("elegoo_neptune_4"),
        "third": _AttrAdapter(None),  # unresolvable → skipped, not ""
    })
    config = tmp_path / "config.yaml"
    config.write_text(textwrap.dedent("""
        active_printer: default
        printers:
          default:
            type: bambu
            printer_model: bambu_a1
          third:
            type: octoprint
            printer_model: prusa_mini
    """))
    monkeypatch.setattr(printer_model_resolver, "_CONFIG_PATH", config)
    models = heartbeat._get_all_printer_models()
    # Adapter models first, config-only models appended, deduped.
    assert models == ["bambu_a1", "elegoo_neptune_4", "prusa_mini"]


def test_all_models_config_only_when_registry_empty(monkeypatch, tmp_path):
    _install_registry(monkeypatch, {})
    config = tmp_path / "config.yaml"
    config.write_text(
        "printers:\n  default:\n    printer_model: creality_k1\n"
    )
    monkeypatch.setattr(printer_model_resolver, "_CONFIG_PATH", config)
    assert heartbeat._get_all_printer_models() == ["creality_k1"]


def test_resolve_all_printer_models_defensive(monkeypatch, tmp_path):
    missing = tmp_path / "nope.yaml"
    monkeypatch.setattr(printer_model_resolver, "_CONFIG_PATH", missing)
    assert printer_model_resolver.resolve_all_printer_models() == []

    bad = tmp_path / "bad.yaml"
    bad.write_text("printers: [not, a, dict]")
    monkeypatch.setattr(printer_model_resolver, "_CONFIG_PATH", bad)
    assert printer_model_resolver.resolve_all_printer_models() == []

    legacy = tmp_path / "legacy.yaml"
    legacy.write_text("printer_model: ender3\n")
    monkeypatch.setattr(printer_model_resolver, "_CONFIG_PATH", legacy)
    assert printer_model_resolver.resolve_all_printer_models() == ["ender3"]


# ---------------------------------------------------------------------------
# Adapter families of the WHOLE fleet — the sibling of the models fix
# ---------------------------------------------------------------------------


class _BambuAdapter:
    """Class name is the family signal, like the real adapters."""


class _MoonrakerAdapter:
    pass


class _OctoPrintAdapter:
    pass


def test_all_adapter_types_sees_every_family(monkeypatch):
    """The top-level adapter_type names only the default printer, so a
    Bambu-default install with a Klipper second machine reported plain
    "bambu" — the mixed fleet's second family was invisible."""
    _install_registry(monkeypatch, {
        "default": _BambuAdapter(),
        "workhorse": _MoonrakerAdapter(),
        "old-faithful": _OctoPrintAdapter(),
    })
    assert heartbeat._get_all_adapter_types() == [
        "bambu", "moonraker", "octoprint",
    ]


def test_all_adapter_types_dedupes_same_family_fleet(monkeypatch):
    _install_registry(monkeypatch, {
        "left": _BambuAdapter(),
        "right": _BambuAdapter(),
    })
    assert heartbeat._get_all_adapter_types() == ["bambu"]


def test_all_adapter_types_and_default_classifier_agree(monkeypatch):
    """One classifier serves both the top-level field and the fleet
    list; if they ever diverge, the dashboard would count the same
    machine under two names."""
    adapter = _MoonrakerAdapter()
    _install_registry(monkeypatch, {"default": adapter})
    monkeypatch.delenv("KILN_PRINTER_TYPE", raising=False)
    monkeypatch.delenv("KILN_PRINTER_MODEL", raising=False)
    _model, adapter_type, _count = heartbeat._get_printer_info()
    assert adapter_type == "moonraker"
    assert heartbeat._get_all_adapter_types() == [adapter_type]


def test_empty_registry_yields_no_families(monkeypatch):
    _install_registry(monkeypatch, {})
    assert heartbeat._get_all_adapter_types() == []
