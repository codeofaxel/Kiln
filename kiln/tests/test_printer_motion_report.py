"""The daily report of how each registered Klipper printer moves.

Right after the heartbeat is sent for the day, every registered printer
whose settings can be read hands over its motion sections
(:mod:`kiln.printer_motion_report`), named by the model the person
declared and fingerprinted the way the service names documents.  Nothing
goes when telemetry is off, when a machine cannot be asked, or when the
heartbeat itself did not land; a failure is silent and never raises.
"""

from __future__ import annotations

import json

from kiln import printer_motion_report as report
from kiln.machine_motion import MOTION_SETTINGS_FORMAT

_CONFIG = {
    "mcu": {"serial": "/dev/serial/by-id/usb-Klipper_stm32f401xc_DEADBEEF-if00"},
    "printer": {"kinematics": "corexy"},
    "stepper_x": {"position_max": "229", "step_pin": "PC14"},
    "gcode_macro PAUSE": {"gcode": "PAUSE_BASE\nG1 Z10"},
}


class _Klipper:
    _printer_model = "k1"

    def __init__(self, config=_CONFIG):
        self._config = config

    def declared_printer_model(self):
        return self._printer_model

    def _read_machine_motion_source(self):
        return ("klipper_config", self._config)


class _Bambu:
    _printer_model = "bambu_a1"

    def declared_printer_model(self):
        return self._printer_model

    def _read_machine_motion_source(self):
        return None


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
    monkeypatch.delenv("KILN_TELEMETRY", raising=False)


class TestGathering:
    def test_each_klipper_printer_reports_under_the_model_the_person_declared(self, monkeypatch):
        _install(monkeypatch, {"k1": _Klipper(), "bambu": _Bambu()})
        reports = report.gather()
        assert [r["p_printer_id"] for r in reports] == ["k1"]
        only = reports[0]
        assert only["p_sections"]["gcode_macro PAUSE"]["gcode"] == "PAUSE_BASE\nG1 Z10"
        assert "step_pin" not in only["p_sections"]["stepper_x"]
        assert only["p_chip"] == "stm32f401xc" and len(only["p_unit"]) == 32
        assert only["p_fingerprint"] == report.fingerprint_of(only["p_sections"])

    def test_the_fingerprint_is_the_services_own(self):
        """The placement service names a document by the same canonical
        text, so a heartbeat's row and a print-beside row are one row."""
        sections = {"b": {"y": "2", "x": "1"}, "a": {"gcode": "G4"}}
        canonical = json.dumps(sections, sort_keys=True, separators=(",", ":")).encode("utf-8")
        import hashlib

        assert report.fingerprint_of(sections) == hashlib.sha256(canonical).hexdigest()

    def test_telemetry_off_gathers_nothing(self, monkeypatch):
        _install(monkeypatch, {"k1": _Klipper()})
        monkeypatch.setenv("KILN_TELEMETRY", "false")
        assert report.gather() == []

    def test_a_machine_that_cannot_be_asked_or_has_no_model_is_skipped(self, monkeypatch):
        class _Broken(_Klipper):
            def _read_machine_motion_source(self):
                raise RuntimeError("offline")

        class _Nameless(_Klipper):
            _printer_model = ""

        _install(monkeypatch, {"a": _Broken(), "b": _Nameless(), "c": _Klipper()})
        assert [r["p_printer_id"] for r in report.gather()] == ["k1"]

    def test_no_registry_is_no_report_and_no_error(self, monkeypatch):
        import kiln.registry as registry_mod

        monkeypatch.setattr(registry_mod, "get_registry", lambda: (_ for _ in ()).throw(RuntimeError("no registry")))
        assert report.gather() == []


class TestSending:
    def _capture(self, monkeypatch, status=200):
        import urllib.request

        posts = []

        class _Resp:
            def __init__(self):
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            posts.append((req.full_url, json.loads(req.data.decode("utf-8")), dict(req.header_items())))
            if status >= 400:
                raise OSError("refused")
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        return posts

    def test_each_report_goes_to_the_services_door_with_the_anon_key(self, monkeypatch):
        posts = self._capture(monkeypatch)
        reports = [{"p_printer_id": "k1", "p_fingerprint": "ab" * 32, "p_sections": {"printer": {}}, "p_unit": None, "p_chip": None}]
        assert report.send(reports, "https://example.supabase.co/", "anon-key") == 1
        url, body, headers = posts[0]
        assert url == "https://example.supabase.co/rest/v1/rpc/record_printer_motion_settings"
        assert body == reports[0] and headers["Apikey"] == "anon-key"

    def test_a_refused_post_is_silent(self, monkeypatch):
        self._capture(monkeypatch, status=500)
        reports = [{"p_printer_id": "k1", "p_fingerprint": "ab" * 32, "p_sections": {"printer": {}}}]
        assert report.send(reports, "https://example.supabase.co", "anon-key") == 0

    def test_after_the_heartbeat_the_registry_is_read_and_sent(self, monkeypatch):
        _install(monkeypatch, {"k1": _Klipper()})
        posts = self._capture(monkeypatch)
        assert report.send_after_heartbeat("https://example.supabase.co", "anon-key") == 1
        assert posts[0][1]["p_printer_id"] == "k1"

    def test_the_document_format_is_the_settings_document(self, monkeypatch):
        from kiln.machine_motion import motion_settings

        assert motion_settings(_CONFIG)["format"] == MOTION_SETTINGS_FORMAT
