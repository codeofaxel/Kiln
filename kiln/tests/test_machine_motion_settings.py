"""What of a printer's own settings leaves the machine, and when.

A Klipper-family printer's configuration decides where its head goes when
it pauses, resumes, cancels or homes.  The placement door judges a print
beside another on THIS unit's macros when the request carries them, so
:func:`kiln.machine_motion.motion_settings` gathers the sections that
decide motion -- and only those, with nothing that identifies the machine
or its owner: no serial numbers, no pins, no paths.  The same switch that
covers the heartbeat covers this (``KILN_TELEMETRY``).
"""

from __future__ import annotations

import json

from kiln import machine_motion
from kiln.machine_motion import MOTION_SETTINGS_FORMAT, motion_settings, motion_settings_of

_CONFIG = {
    "mcu": {"serial": "/dev/serial/by-id/usb-Klipper_stm32f401xc_1A0032000B51313339373836-if00",
            "restart_method": "command"},
    "mcu nozzle_mcu": {"serial": "/dev/serial/by-id/usb-Klipper_rp2040_E661AC4863399B21-if00"},
    "printer": {"kinematics": "corexy", "max_velocity": "800"},
    "stepper_x": {"step_pin": "PC14", "dir_pin": "!PC13", "enable_pin": "!PC15", "endstop_pin": "tmc2209_stepper_x:virtual_endstop",
                  "position_endstop": "229", "position_max": "229", "homing_speed": "36"},
    "extruder": {"step_pin": "PB1", "min_extrude_temp": "170", "pressure_advance": "0.04"},
    "gcode_macro PAUSE": {"rename_existing": "PAUSE_BASE",
                          "gcode": "PAUSE_BASE\nG91\nG1 Z10\nG90\nG1 X219 Y113\nSAVE_VARIABLE VARIABLE=last_park VALUE=1"},
    "delayed_gcode wait_temp": {"gcode": "UPDATE_DELAYED_GCODE ID=wait_temp DURATION=1"},
    "idle_timeout": {"timeout": "600"},
    "save_variables": {"filename": "/usr/data/printer_data/config/saved_variables.cfg"},
    "virtual_sdcard": {"path": "/home/pi/printer_data/gcodes"},
    "heater_bed": {"heater_pin": "PA0", "sensor_type": "EPCOS 100K B57560G104F"},
    "output_pin fan2": {"pin": "PB9", "pwm": "True"},
    "z_tilt": {"z_positions": "-10, 110\n230, 110", "points": "30, 110\n200, 110", "speed": "200"},
}


class TestWhatLeaves:
    def test_the_motion_sections_leave_and_nothing_else(self):
        doc = motion_settings(_CONFIG)
        assert doc["format"] == MOTION_SETTINGS_FORMAT
        assert set(doc["sections"]) == {"printer", "stepper_x", "extruder", "gcode_macro PAUSE", "delayed_gcode wait_temp",
                                        "idle_timeout", "z_tilt"}

    def test_serial_numbers_pins_and_paths_never_leave(self):
        text = json.dumps(motion_settings(_CONFIG))
        for secret in ("1A0032000B51313339373836", "E661AC4863399B21", "/dev/serial", "/usr/data", "/home/pi",
                       "PC14", "PB1", "PB9", "tmc2209_stepper_x"):
            assert secret not in text, secret

    def test_the_macro_text_leaves_whole_including_a_slash_inside_it(self):
        config = dict(_CONFIG)
        config["gcode_macro M117"] = {"gcode": "RESPOND MSG=\"1/2 done\""}
        assert motion_settings(config)["sections"]["gcode_macro M117"]["gcode"] == "RESPOND MSG=\"1/2 done\""

    def test_the_board_is_named_by_its_chip_and_the_unit_by_a_one_way_hash(self):
        doc = motion_settings(_CONFIG)
        assert doc["chip"] == "stm32f401xc"
        assert len(doc["unit"]) == 32 and "1A0032000B51313339373836" not in doc["unit"]
        again = motion_settings(_CONFIG)
        assert again["unit"] == doc["unit"], "the same printer counts once"
        other = dict(_CONFIG)
        other["mcu"] = {"serial": "/dev/serial/by-id/usb-Klipper_stm32f401xc_FFFFFFFF-if00"}
        assert motion_settings(other)["unit"] != doc["unit"]

    def test_a_configuration_with_no_motion_sections_sends_nothing(self):
        assert motion_settings({"heater_bed": {"heater_pin": "PA0"}}) is None
        assert motion_settings(None) is None
        assert motion_settings("text") is None

    def test_a_document_too_large_to_send_sends_nothing(self):
        huge = dict(_CONFIG)
        huge["gcode_macro BIG"] = {"gcode": "G4 P1\n" * 200_000}
        assert motion_settings(huge) is None


class _Machine:
    def __init__(self, source):
        self._source = source

    def _read_machine_motion_source(self):
        return self._source


class TestWhen:
    def test_a_klipper_machine_hands_over_its_settings_while_telemetry_is_on(self, monkeypatch):
        monkeypatch.delenv("KILN_TELEMETRY", raising=False)
        doc = motion_settings_of(_Machine(("klipper_config", _CONFIG)))
        assert doc and "gcode_macro PAUSE" in doc["sections"]

    def test_telemetry_off_sends_nothing(self, monkeypatch):
        monkeypatch.setenv("KILN_TELEMETRY", "false")
        assert motion_settings_of(_Machine(("klipper_config", _CONFIG))) is None

    def test_a_machine_that_is_not_klipper_or_cannot_be_asked_sends_nothing(self, monkeypatch):
        monkeypatch.delenv("KILN_TELEMETRY", raising=False)
        assert motion_settings_of(_Machine(("marlin_report", object()))) is None
        assert motion_settings_of(_Machine(None)) is None
        assert motion_settings_of(object()) is None

        class _Broken:
            def _read_machine_motion_source(self):
                raise RuntimeError("offline")

        assert motion_settings_of(_Broken()) is None

    def test_the_placement_request_carries_them(self, monkeypatch):
        from kiln import _pro_placement_bridge as bridge

        monkeypatch.delenv("KILN_TELEMETRY", raising=False)
        monkeypatch.setattr(machine_motion, "motion_settings_of", lambda adapter: {"format": MOTION_SETTINGS_FORMAT,
                                                                                    "sections": {"printer": {"kinematics": "corexy"}},
                                                                                    "chip": None, "unit": None})

        class _State:
            status, job, jobs, since, occupied = "clear", None, [], None, False

        monkeypatch.setattr("kiln.plate_state.read", lambda adapter: _State())
        req = bridge.request_for(object(), "k1", placement="auto", part={"size_mm": [10, 10, 10]})
        assert req["printer_settings"]["sections"] == {"printer": {"kinematics": "corexy"}}
