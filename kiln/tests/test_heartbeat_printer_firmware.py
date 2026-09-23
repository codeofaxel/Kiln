"""Which firmware the printers out in the wild report they run.

The daily heartbeat carries, for every registered printer that reports
one, the firmware version it runs -- version text only, cleaned to a
short label -- through the one accessor every backend answers from what
it already holds (``reported_firmware_version``).  The point: a maker's
published code is read for one version, and the machines may run
another; the MK4's record was once read from a version no MK4 ships.
"""

from __future__ import annotations

from kiln import heartbeat
from kiln.printers.base import PrinterAdapter


class _Registry:
    def __init__(self, adapters):
        self._adapters = adapters

    def list_names(self):
        return list(self._adapters)

    def get(self, name):
        return self._adapters[name]


def _install(monkeypatch, adapters):
    import kiln.registry as registry_mod

    monkeypatch.setattr(registry_mod, "get_registry", lambda: _Registry(adapters))


class _Printer:
    def __init__(self, model, version):
        self.printer_model = model
        self._version = version

    def reported_firmware_version(self):
        return self._version


class TestWhatTheHeartbeatCarries:
    def test_each_printer_that_reports_a_version_is_listed_with_its_model(self, monkeypatch):
        _install(monkeypatch, {
            "a": _Printer("Bambu Lab A1", "01.05.00.00"),
            "b": _Printer("Creality K1", "v0.12.0-123-gabcdef1"),
            "c": _Printer("Prusa MK4", None),
        })
        assert heartbeat._get_printer_firmware() == [
            {"model": "Bambu Lab A1", "firmware": "01.05.00.00"},
            {"model": "Creality K1", "firmware": "v0.12.0-123-gabcdef1"},
        ]

    def test_a_version_is_a_short_clean_label_never_free_text(self, monkeypatch):
        _install(monkeypatch, {"a": _Printer("X", "v1.2\n<script>" + "9" * 100)})
        [row] = heartbeat._get_printer_firmware()
        assert "\n" not in row["firmware"] and "<" not in row["firmware"]
        assert len(row["firmware"]) <= 64

    def test_a_printer_that_cannot_be_asked_is_skipped(self, monkeypatch):
        class _Broken(_Printer):
            def reported_firmware_version(self):
                raise RuntimeError("offline")

        _install(monkeypatch, {"a": _Broken("X", "1"), "b": _Printer("Y", "2"), "c": object()})
        assert heartbeat._get_printer_firmware() == [{"model": "Y", "firmware": "2"}]

    def test_the_list_is_capped_like_the_model_list(self, monkeypatch):
        _install(monkeypatch, {str(i): _Printer(f"M{i}", f"{i}.0") for i in range(20)})
        assert len(heartbeat._get_printer_firmware()) == heartbeat._MAX_HEARTBEAT_PRINTER_MODELS

    def test_no_registry_is_an_empty_list(self, monkeypatch):
        import kiln.registry as registry_mod

        monkeypatch.setattr(registry_mod, "get_registry", lambda: (_ for _ in ()).throw(RuntimeError("none")))
        assert heartbeat._get_printer_firmware() == []


class TestTheAccessor:
    def test_the_base_answers_from_a_marlin_report_it_already_read(self):
        class _Report:
            firmware_name = "Marlin"
            firmware_version = "2.1.2.1"

        class _Bare:
            """A backend with no answer of its own: the base accessor reads
            only what the motion facts already read off the machine."""

        adapter = _Bare()
        assert PrinterAdapter.reported_firmware_version(adapter) is None
        adapter._motion_machine_source = ("marlin_report", _Report())
        assert PrinterAdapter.reported_firmware_version(adapter) == "Marlin 2.1.2.1"

    def test_a_klipper_config_source_is_not_a_version(self):
        class _Bare:
            _motion_machine_source = ("klipper_config", {"printer": {}})

        assert PrinterAdapter.reported_firmware_version(_Bare()) is None
