"""A Klipper printer's heater checks, read and kept on this computer.

A served heat check judges whether a printer's ``verify_heater`` settings
were loosened since the last different reading.  Kiln's servers run no
printer and keep nothing, so the reading and its history live here: read
at every print start (never holding the print up) and whenever a heat check
asks, one entry per distinct state.  What leaves is heater names, four
numbers each and the firmware version -- never the rest of the
configuration, the address, or the machine's identity.
"""

from __future__ import annotations

import json
import threading

import pytest

from kiln import heater_check_readings as readings
from kiln import served_steps

CONFIG = {
    "mcu": {"serial": "/dev/serial/by-id/usb-Klipper_stm32-if00"},
    "gcode_macro NOTIFY": {"gcode": "RUN_SHELL_COMMAND CMD=curl https://hooks.example.com/?t=tok_live"},
    "extruder": {"heater_pin": "PA1", "max_temp": "300"},
    "heater_bed": {"heater_pin": "PA2"},
    "verify_heater extruder": {"max_error": "240", "check_gain_time": "40", "pin": "^PG15", "note": "10.0.0.5"},
}


class _Klipper:
    def __init__(self, serial: str, config=CONFIG, version="v0.12.0-251-g6d9cbc4b", boom=None):
        self.serial, self.config, self.version, self.boom = serial, config, version, boom

    def get_printer_config(self):
        if self.boom:
            raise self.boom
        return self.config

    def reported_firmware_version(self):
        return self.version


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    import kiln.registry as registry

    monkeypatch.setattr(registry, "machine_fingerprint", lambda adapter: f"klipper:serial:{adapter.serial}")
    return tmp_path


class TestWhatIsRead:
    def test_only_heater_names_their_four_settings_and_the_firmware_are_kept(self):
        state = readings.state_of(CONFIG, "v0.12.0-251-g6d9cbc4b")
        assert state == {
            "heaters": ["extruder", "heater_bed"],
            "verify_heater": {"extruder": {"max_error": 240.0, "check_gain_time": 40.0}},
            "firmware_version": "v0.12.0-251-g6d9cbc4b",
        }
        sent = json.dumps(state)
        for never in ("serial", "stm32", "hooks.example.com", "tok_live", "PA1", "^PG15", "10.0.0.5", "max_temp"):
            assert never not in sent, never

    @pytest.mark.parametrize("version", ["v0.12 at 10.0.0.5", "x" * 70, 12])
    def test_a_firmware_word_that_is_not_a_version_is_dropped(self, version):
        assert readings.state_of(CONFIG, version)["firmware_version"] is None

    @pytest.mark.parametrize("config", [None, [], {"mcu": {}}, {"printer": {"kinematics": "corexy"}}])
    def test_a_configuration_with_no_heater_is_none(self, config):
        assert readings.state_of(config, "v1") is None


class TestWhatIsKept:
    def test_a_changed_state_is_a_new_entry_and_the_last_different_one_comes_with_it(self):
        voron = _Klipper("A1")
        first = readings.remember(voron, readings.read_state(voron))
        assert first["previous"] is None and first["watched_since"] == first["current"]["first_seen"]

        again = readings.remember(voron, readings.read_state(voron))
        assert again["previous"] is None, "the same state twice is one entry, seen again"

        voron.config = {**CONFIG, "verify_heater extruder": {"max_error": "9999"}}
        changed = readings.remember(voron, readings.read_state(voron))
        assert changed["previous"]["verify_heater"] == {"extruder": {"max_error": 240.0, "check_gain_time": 40.0}}
        assert changed["current"]["verify_heater"] == {"extruder": {"max_error": 9999.0}}
        assert changed["watched_since"] == first["watched_since"]
        assert readings.stored(voron)["current"] == changed["current"]

    def test_history_is_per_machine_never_per_name(self):
        readings.remember(_Klipper("A1"), readings.state_of(CONFIG, "v1"))
        assert readings.stored(_Klipper("B2")) is None

    def test_history_is_bounded(self):
        voron = _Klipper("A1")
        for n in range(readings.HISTORY_LIMIT + 5):
            readings.remember(voron, readings.state_of({**CONFIG, "verify_heater extruder": {"max_error": str(n)}}, "v1"))
        kept = json.loads((readings._store_path()).read_text())["machines"]["klipper:serial:A1"]
        assert len(kept) == readings.HISTORY_LIMIT

    def test_kilns_servers_keep_nothing(self, monkeypatch):
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        view = readings.remember(_Klipper("A1"), readings.state_of(CONFIG, "v1"))
        assert view["previous"] is None and view["watched_since"] is None
        assert not readings._store_path().exists()


class TestAtPrintStart:
    def test_the_reading_is_kept_on_a_thread_of_its_own(self):
        voron = _Klipper("A1")
        started = threading.Event()
        real = readings.remember

        def remember(adapter, state):
            view = real(adapter, state)
            started.set()
            return view

        readings.remember = remember
        try:
            readings.note_print_started(voron)
            assert started.wait(5), "the reading was never kept"
        finally:
            readings.remember = real
        assert readings.stored(voron)["current"]["heaters"] == ["extruder", "heater_bed"]

    def test_a_printer_that_cannot_answer_never_reaches_the_print(self):
        readings.note_print_started(_Klipper("A1", boom=OSError("no route to 10.0.0.5")))
        readings.note_print_started(object())  # not a Klipper printer: nothing to read


class TestServedFacts:
    def test_every_klipper_printer_is_read_and_one_that_cannot_answer_says_so_with_what_was_kept(self, monkeypatch):
        import kiln.server as server

        voron, trident = _Klipper("A1"), _Klipper("B2")
        readings.remember(trident, readings.state_of(CONFIG, "v1"))
        trident.boom = OSError("no route to 10.0.0.9")
        adapters = {"voron": voron, "trident": trident, "a1": object()}
        monkeypatch.setattr(server, "_read_config_printers", lambda: {
            "voron": {"type": "moonraker", "printer_model": "voron_2", "host": "voron.local"},
            "trident": {"type": "moonraker", "printer_model": "voron_trident"},
            "a1": {"type": "bambu", "printer_model": "bambu_a1", "serial": "01P00A000000001"},
        })
        monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda name=None: "a1")
        monkeypatch.setattr(server, "_get_registry", lambda: type("R", (), {"get": lambda self, n: adapters[n]})())

        done, refused = served_steps.read_first(
            "fleet_thermal_posture", [{"kind": "printer_facts", "want": ["every_heater_checks"]}], printer_name="",
        )
        assert refused is None
        by_name = {p["name"]: p for p in done["printer_facts"]["data"]["printers"]}
        assert by_name["voron"]["heater_checks"]["read_now"] is True
        assert by_name["voron"]["heater_checks"]["current"]["verify_heater"]["extruder"]["max_error"] == 240.0
        assert by_name["trident"]["heater_checks"]["read_now"] is False
        assert by_name["trident"]["heater_checks"]["current"]["heaters"] == ["extruder", "heater_bed"]
        assert by_name["trident"]["heater_checks_error"]
        assert "heater_checks" not in by_name["a1"], "only Klipper printers keep these settings"
        sent = json.dumps(done)
        for never in ("klipper:serial", "voron.local", "10.0.0.9", "01P00A000000001", "stm32"):
            assert never not in sent, never
