"""A Marlin printer's own reports settle what its maker never published.

Three report-only commands (M115, M211, M119) and what each can and cannot
settle.  The transcripts are verbatim from the Marlin sources and docs the
module cites; the format traps (an ``echo:`` prefix, ``ON`` vs ``On``, a
probe that shares the Z-min pin) each get a case.
"""
from __future__ import annotations

import pytest

from kiln.machine_motion import fill_from_marlin_report
from kiln.motion_facts import MotionFacts, MotionSource, motion_facts_for
from kiln.printers.base import PlateClearRequired, PrinterState, PrinterStatus
from kiln.printers.command_verdict import CommandVerdict
from kiln.printers.marlin_report import (
    MarlinMotionReport,
    parse_m115,
    parse_m119,
    parse_m211,
    read_marlin_motion_report,
)


# --- transcripts, as they arrive on the serial line ------------------------
# Every shape below is derived from the Marlin sources at the cited tag (the
# reference is scratchpad/marlin_reports/MARLIN_REPORTS.md of 2026-09-17).

# M115: gcode/host/M115.cpp:63-75 @ 2.1.2.4; Cap: lines :94-172, Z_PROBE = HAS_BED_PROBE (:136).
M115_2X = (
    "FIRMWARE_NAME:Marlin 2.1.2.4 (Jun 15 2024 12:00:00) SOURCE_CODE_URL:github.com/MarlinFirmware/Marlin "
    "PROTOCOL_VERSION:1.0 MACHINE_TYPE:Ender-3 EXTRUDER_COUNT:1 UUID:cede2a2f-41a2-4748-9b12-c55c62f367ff\n"
    "Cap:SERIAL_XON_XOFF:0\nCap:EEPROM:1\nCap:AUTOREPORT_TEMP:1\nCap:Z_PROBE:1\nCap:LEVELING_DATA:1\n"
    "Cap:SOFTWARE_POWER:0\nCap:HOST_ACTION_COMMANDS:1\nok"
)
M115_2X_NO_PROBE = M115_2X.replace("Cap:Z_PROBE:1", "Cap:Z_PROBE:0")
# 1.1.9.1: language.h:142 MSG_M115_REPORT, Version.h:38-86 -- "(Github)", no build stamp, no Cap: lines.
M115_1X = "FIRMWARE_NAME:Marlin 1.1.9.1 (Github) SOURCE_CODE_URL:https://github.com/MarlinFirmware/Marlin PROTOCOL_VERSION:1.0 MACHINE_TYPE:3D Printer EXTRUDER_COUNT:1 UUID:cede2a2f-41a2-4748-9b12-c55c62f367ff\nok"
# Creality Ender-3 1.1.6.1 tree: Version.h:44 DETAILED_BUILD_VERSION "Creality 3D" -- no version number at all.
M115_E3 = "FIRMWARE_NAME:Marlin Creality 3D SOURCE_CODE_URL:https://github.com/MarlinFirmware/Marlin PROTOCOL_VERSION:1.0 MACHINE_TYPE:Ender-3 EXTRUDER_COUNT:1 UUID:cede2a2f-41a2-4748-9b12-c55c62f367ff\nok"
# Creality Ender-3 V2 tree (2.0.8.x): src/inc/Version.h:28-29 SHORT_BUILD_VERSION "V1.0.4", 2.x build stamp.
M115_E3V2 = "FIRMWARE_NAME:Marlin V1.0.4 (Mar  3 2021 10:00:00) SOURCE_CODE_URL:github.com/MarlinFirmware/Marlin PROTOCOL_VERSION:1.0 MACHINE_TYPE:Ender-3 V2 EXTRUDER_COUNT:1 UUID:cede2a2f-41a2-4748-9b12-c55c62f367ff\nok"
# Prusa-Firmware 3.14.1: Marlin_main.cpp:5794-5817 -- FIRMWARE_URL, no UUID, no Z_PROBE capability.
M115_PRUSA = "FIRMWARE_NAME:Prusa-Firmware 3.14.1+8237_abcdef0 based on Marlin FIRMWARE_URL:https://github.com/prusa3d/Prusa-Firmware PROTOCOL_VERSION:1.0 MACHINE_TYPE:Prusa i3 MK3S EXTRUDER_COUNT:1\nCap:AUTOREPORT_TEMP:1\nCap:AUTOREPORT_POSITION:1\nCap:PRUSA_MMU2:1\nok"

# M211, 2.0.9.2 and later: M211.cpp:42-52 @ 2.1.2.4 -- replayable line, then both triples
# (STR_SOFT_MIN "  Min: ", STR_SOFT_MAX "  Max: ", SP_X_STR " X": two spaces before X, three before Max).
M211_2X = "  M211 S1 ; ON\n  Min:  X0.00 Y0.00 Z0.00   Max:  X220.00 Y220.00 Z250.00\nok"
M211_2X_OFF = "  M211 S0 ; OFF\n  Min:  X-5.00 Y-5.00 Z0.00   Max:  X300.00 Y300.00 Z400.00\nok"
# M211, 2.0.0 to 2.0.9.1 and the Ender-3 V2 / AnkerMake M5 trees: one echo line (M211.cpp:35-45 @ 2.0.9.1).
M211_20X = "echo:Soft endstops: ON  Min:  X0.00 Y0.00 Z0.00   Max:  X220.00 Y220.00 Z250.00\nok"
# M211, 1.1.9.1: Marlin_main.cpp:9989-10007 -- MSG_ON "On " (trailing space), no space before X.
M211_1X = "echo:Soft endstops: On   Min: X0.00 Y0.00 Z0.00  Max: X200.00 Y200.00 Z200.00\nok"
M211_1X_OFF = "echo:Soft endstops: Off  Min: X0.00 Y0.00 Z0.00  Max: X200.00 Y200.00 Z200.00\nok"
# A German 1.1.x build: the state word is the LCD language's (language_de.h:190-191).
M211_1X_DE = "echo:Soft endstops: Ein   Min: X0.00 Y0.00 Z0.00  Max: X200.00 Y200.00 Z200.00\nok"
M211_MISSING = 'echo:Unknown command: "M211"\nok'
M211_PRUSA = "Unknown M code: M211\nok"

# M119: endstops.cpp:577-691 @ 2.1.2.4 -- no echo:, lower-case labels, open / TRIGGERED.
M119_SWITCH = "Reporting endstop status\nx_min: open\ny_min: open\nz_min: TRIGGERED\nok"
M119_SEPARATE_PROBE = "Reporting endstop status\nx_min: open\ny_min: open\nz_min: open\nz_probe: open\nok"
M119_TOP = "Reporting endstop status\nx_min: open\ny_min: open\nz_max: open\nok"
# Prusa MK3S prints all six pins although Z homes to MIN (Marlin_main.cpp:5889-5947).
M119_MK3S = "Reporting endstop status\nx_min: open\nx_max: open\ny_min: open\ny_max: open\nz_min: open\nz_max: open\nok"


class TestTheParsers:
    def test_m115_identity_and_capabilities(self):
        assert parse_m115(M115_2X) == ("Marlin", "2.1.2.4", {
            "SERIAL_XON_XOFF": False, "EEPROM": True, "AUTOREPORT_TEMP": True,
            "Z_PROBE": True, "LEVELING_DATA": True, "SOFTWARE_POWER": False, "HOST_ACTION_COMMANDS": True,
        })
        assert parse_m115(M115_1X)[:2] == ("Marlin", "1.1.9.1")
        assert parse_m115(M115_PRUSA)[:2] == ("Prusa-Firmware", None)   # 3.14.1+8237_hash is not a Marlin version
        assert parse_m115(M115_PRUSA)[0] == "Prusa-Firmware"
        assert parse_m115("ok") == (None, None, {})

    def test_a_vendor_mangled_version_names_no_version(self):
        assert parse_m115(M115_E3)[:2] == ("Marlin", None)
        assert parse_m115(M115_E3V2)[:2] == ("Marlin", None)
        assert parse_m115("FIRMWARE_NAME:Marlin bugfix-2.1.x (Jan  1 2024 00:00:00) SOURCE_CODE_URL:x\nok")[:2] == ("Marlin", "bugfix-2.1.x")

    def test_m211_limits_in_all_three_shapes_and_when_off(self):
        assert parse_m211(M211_2X) == (True, 0.0, 250.0)
        assert parse_m211(M211_2X_OFF) == (False, 0.0, 400.0)
        assert parse_m211(M211_20X) == (True, 0.0, 250.0)
        assert parse_m211(M211_1X) == (True, 0.0, 200.0)
        assert parse_m211(M211_1X_OFF) == (False, 0.0, 200.0)
        assert parse_m211(M211_1X_DE) == (None, 0.0, 200.0)   # state unknown, limits still valid
        assert parse_m211(M211_MISSING) == (None, None, None)
        assert parse_m211(M211_PRUSA) == (None, None, None)

    def test_m119_lists_the_endstop_pins_not_their_state(self):
        assert parse_m119(M119_SEPARATE_PROBE) == {"x_min", "y_min", "z_min", "z_probe"}
        assert parse_m119(M119_TOP) == {"x_min", "y_min", "z_max"}
        assert parse_m119(M119_MK3S) == {"x_min", "x_max", "y_min", "y_max", "z_min", "z_max"}
        assert parse_m119('echo:Unknown command: "M119"\nok') == frozenset()

    def test_the_reader_tries_each_command_on_its_own(self):
        answers = {"M115": M115_2X_NO_PROBE, "M211": M211_MISSING}

        def query(cmd):
            if cmd == "M119":
                raise RuntimeError("timeout")
            return answers[cmd]

        report = read_marlin_motion_report(query)
        assert report.firmware_version == "2.1.2.4" and report.z_max is None and report.endstops == frozenset()
        assert report.marlin_2_markers is True
        assert set(report.raw) == {"M115", "M211"}

    def test_m119_is_never_asked_of_a_machine_with_a_probe(self):
        """On a BLTouch build M119 puts the probe in SW mode and moves the pin
        (endstops.cpp:578,689 @ 2.1.2.4); Kiln asks only a build that said Cap:Z_PROBE:0."""
        asked: list[str] = []

        def query(cmd):
            asked.append(cmd)
            return {"M115": M115_2X, "M211": M211_2X, "M119": M119_SEPARATE_PROBE}[cmd]

        report = read_marlin_motion_report(query)
        assert asked == ["M115", "M211"] and report.endstops == frozenset()
        asked.clear()
        read_marlin_motion_report(lambda cmd: {"M115": M115_1X, "M211": M211_1X, "M119": M119_SWITCH}[cmd])
        assert "M119" not in asked  # no Cap: lines at all: not asked either

    def test_a_silent_line_is_no_report(self):
        assert read_marlin_motion_report(lambda cmd: "ok") is None


def _report(**kw) -> MarlinMotionReport:
    return MarlinMotionReport(**kw)


def _row(**kw) -> MotionFacts:
    return MotionFacts(printer_id=kw.pop("printer_id", "x"), **kw)


class TestWhatTheReportsSettle:
    def test_z_ceiling_and_family(self):
        facts = fill_from_marlin_report(_row(z_carrier="head"), _report(firmware_name="Marlin", firmware_version="2.1.2.1", z_max=250.0))
        assert facts.z_travel_limit_mm == 250.0 and facts.z_travel_limit_kind == "firmware_config"
        assert facts.firmware_family == "marlin_2"
        assert facts.source("z_travel_limit_mm").source_class == "machine_config"
        assert "M211" in facts.source("z_travel_limit_mm").note

    def test_family_buckets(self):
        assert fill_from_marlin_report(_row(), _report(firmware_name="Marlin", firmware_version="1.1.9.1")).firmware_family == "marlin_1"
        assert fill_from_marlin_report(_row(), _report(firmware_name="Prusa-Firmware")).firmware_family == "prusa_firmware"
        assert fill_from_marlin_report(_row(), _report(firmware_name="Marlin", firmware_version="bugfix-2.1.x")).firmware_family == "marlin_2"
        # a mangled version names no family on its own; a 2.x marker does
        assert fill_from_marlin_report(_row(), _report(firmware_name="Marlin")).firmware_family is None
        assert fill_from_marlin_report(_row(), _report(firmware_name="Marlin", marlin_2_markers=True)).firmware_family == "marlin_2"
        assert fill_from_marlin_report(_row(), _report(firmware_name="Klipper", firmware_version="0.12")).firmware_family is None

    def test_the_vendor_trees_end_to_end(self):
        e3 = read_marlin_motion_report(lambda cmd: {"M115": M115_E3, "M211": M211_1X}.get(cmd, "ok"))
        assert fill_from_marlin_report(_row(), e3).firmware_family is None       # Creality 3D: no marker
        e3v2 = read_marlin_motion_report(lambda cmd: {"M115": M115_E3V2, "M211": M211_20X}.get(cmd, "ok"))
        assert fill_from_marlin_report(_row(), e3v2).firmware_family == "marlin_2"   # the build stamp
        prusa = read_marlin_motion_report(lambda cmd: {"M115": M115_PRUSA, "M211": M211_PRUSA}.get(cmd, "ok"))
        facts = fill_from_marlin_report(_row(), prusa)
        assert facts.firmware_family == "prusa_firmware" and facts.z_travel_limit_mm is None

    def test_a_z_min_switch_with_no_probe_is_the_switch_at_plate_height(self):
        facts = fill_from_marlin_report(
            _row(z_carrier="head"),
            _report(capabilities={"Z_PROBE": False}, endstops=frozenset({"x_min", "y_min", "z_min"})),
        )
        assert facts.z_home_method == "endstop_switch" and facts.z_home_descends_onto_plate
        assert "Cap:Z_PROBE:0" in facts.source("z_home_method").note

    def test_a_machine_with_a_probe_keeps_the_refusing_default(self):
        """The reader never asks M119 of a probe-equipped build (it moves a BLTouch
        pin), and whether Z homes on the probe is a compile-time choice anyway."""
        for endstops in (frozenset(), frozenset({"x_min", "y_min", "z_min"}), frozenset({"x_min", "y_min", "z_min", "z_probe"})):
            facts = fill_from_marlin_report(_row(z_carrier="head"), _report(capabilities={"Z_PROBE": True}, endstops=endstops))
            assert facts.z_home_method is None and facts.z_home_descends_onto_plate

    def test_no_capability_lines_settle_no_method(self):
        facts = fill_from_marlin_report(_row(z_carrier="bed"), _report(endstops=frozenset({"x_min", "y_min", "z_max"})))
        assert facts.z_home_method is None

    def test_both_z_pins_listed_is_the_refusing_reading(self):
        """A Z-max pin beside a Z-min pin proves nothing about direction (the MK3S
        lists all six while homing down); the switch at plate height is the reading."""
        both = _report(capabilities={"Z_PROBE": False}, endstops=frozenset({"x_min", "y_min", "z_min", "z_max"}))
        facts = fill_from_marlin_report(_row(z_carrier="head"), both)
        assert facts.z_home_method == "endstop_switch" and facts.z_home_descends_onto_plate

    def test_a_z_max_switch_alone_is_the_far_end_named_by_the_carrier(self):
        top = _report(capabilities={"Z_PROBE": False}, endstops=frozenset({"x_min", "y_min", "z_max"}))
        assert fill_from_marlin_report(_row(z_carrier="head"), top).z_home_method == "endstop_switch_top"
        assert fill_from_marlin_report(_row(z_carrier="bed"), top).z_home_method == "endstop_switch_off_plate"
        assert fill_from_marlin_report(_row(), top).z_home_method is None

    def test_the_vendors_fact_outranks_the_report(self):
        vendor = MotionFacts(printer_id="x", z_travel_limit_mm=300.0,
                             sources={"z_travel_limit_mm": MotionSource("vendor_config")})
        facts = fill_from_marlin_report(vendor, _report(z_max=250.0))
        assert facts.z_travel_limit_mm == 300.0 and facts.machine_read_fields == ()

    def test_soft_endstops_off_is_the_one_reading_that_outranks_the_vendor(self):
        off = _report(soft_endstops_on=False, z_max=250.0)
        clamped = MotionFacts(printer_id="x", unhomed_move_policy="clamped",
                              sources={"unhomed_move_policy": MotionSource("vendor_config")})
        facts = fill_from_marlin_report(clamped, off)
        assert facts.unhomed_move_policy == "unclamped"
        assert "the vendor's record said clamped" in facts.source("unhomed_move_policy").note
        assert "soft limits will not catch" in facts.blind_travel_caveat()
        assert fill_from_marlin_report(_row(), off).unhomed_move_policy == "unclamped"
        # ON settles nothing: a refusal or a clamp, and the report cannot say which
        assert fill_from_marlin_report(_row(), _report(soft_endstops_on=True)).unhomed_move_policy is None
        refused = MotionFacts(printer_id="x", unhomed_move_policy="refused", z_travel_limit_mm=1.0, z_home_method="endstop_switch",
                              z_home_xy_mm=(0.0, 0.0), home_routine_travels_blind=False, firmware_family="marlin_2", z_travel_limit_kind="firmware_config")
        assert fill_from_marlin_report(refused, off).unhomed_move_policy == "refused"

    def test_what_no_report_can_settle_stays_null(self):
        full = _report(firmware_name="Marlin", firmware_version="2.1.2.4", z_max=250.0, soft_endstops_on=True,
                       capabilities={"Z_PROBE": False}, endstops=frozenset({"x_min", "y_min", "z_min"}))
        facts = fill_from_marlin_report(_row(z_carrier="head"), full)
        assert facts.z_home_xy_mm is None and facts.home_routine_travels_blind is None
        assert facts.unhomed_move_policy is None and facts.park_verb is None


class TestTheSerialDoor:
    """An Ender 5 on USB: the catalogue has no Z ceiling and no firmware family; the printer does."""

    def _adapter(self, monkeypatch, answers: dict[str, str], status=PrinterStatus.IDLE):
        # pyserial is an optional extra; the adapter only imports it, so a
        # stub module is enough to build one without a port.
        import sys
        import types

        from unittest import mock

        stub = types.ModuleType("serial")
        stub.Serial = mock.MagicMock()
        stub.SerialException = Exception
        monkeypatch.setitem(sys.modules, "serial", stub)
        from kiln.printers.serial_adapter import SerialPrinterAdapter

        monkeypatch.setattr(SerialPrinterAdapter, "_wait_for_startup", lambda self: None)
        monkeypatch.setattr(SerialPrinterAdapter, "_capture_machine_type", lambda self: None)
        adapter = SerialPrinterAdapter(port="/dev/ttyUSB0")
        adapter.set_safety_profile("ender5")
        monkeypatch.setattr(adapter, "get_state",
                            lambda: PrinterState(connected=True, state=status, tool_temp_actual=25.0))
        sent: list[str] = []

        def send(cmd, **kw):
            sent.append(cmd)
            return answers.get(cmd, "ok")

        monkeypatch.setattr(adapter, "_send_command", send)
        monkeypatch.setattr(adapter, "_plate_witness", lambda: None)
        return adapter, sent

    def test_the_reports_reach_the_gate_and_the_plan(self, monkeypatch):
        adapter, sent = self._adapter(monkeypatch, {"M115": M115_1X, "M211": "echo:Soft endstops: On  Min: X0.00 Y0.00 Z0.00  Max: X220.00 Y220.00 Z300.00\nok", "M119": M119_SWITCH})
        # M115_1X carries no Cap: lines: the Ender 5's switch stays the vendor's word, never the report's
        facts = adapter.motion_facts()
        assert facts.z_travel_limit_mm == 300.0 and facts.firmware_family == "marlin_1"
        assert facts.z_home_method == "endstop_switch"  # the vendor's, untouched
        assert set(facts.machine_read_fields) == {"z_travel_limit_mm", "z_travel_limit_kind", "firmware_family"}
        assert sent.count("M211") == 1
        adapter.motion_facts()
        assert sent.count("M211") == 1, "read once per adapter"
        with pytest.raises(PlateClearRequired):
            adapter.home_axes()   # the Ender 5's Z-min switch is at plate height: still asks
        plan = adapter.home_axes(plan_only=True, plate_clear=True)
        assert "read off this machine itself: firmware_family, z_travel_limit_mm, z_travel_limit_kind" in plan.steps[0]["you_will_see"]

    def test_nothing_is_asked_during_a_print_and_the_next_idle_call_asks(self, monkeypatch):
        adapter, sent = self._adapter(monkeypatch, {"M115": M115_1X, "M211": M211_1X}, status=PrinterStatus.PRINTING)
        assert adapter.motion_facts().machine_read_fields == ()
        assert sent == []
        monkeypatch.setattr(adapter, "get_state",
                            lambda: PrinterState(connected=True, state=PrinterStatus.IDLE, tool_temp_actual=25.0))
        assert adapter.motion_facts().z_travel_limit_mm == 200.0

    def test_a_firmware_without_the_commands_settles_nothing(self, monkeypatch):
        adapter, _ = self._adapter(monkeypatch, {"M115": "ok", "M211": M211_MISSING, "M119": 'echo:Unknown command: "M119"\nok'})
        assert adapter.motion_facts().machine_read_fields == ()
        assert motion_facts_for("ender5").z_travel_limit_mm is None  # the catalogue is untouched


def test_the_doctor_names_what_it_read_off_the_machine(monkeypatch):
    """`kiln doctor` on a USB Ender 5 says which cells came from the printer."""
    from kiln.cli.main import _doctor_homing_how

    adapter, _ = TestTheSerialDoor()._adapter(monkeypatch, {"M115": M115_1X, "M211": M211_2X, "M119": M119_SWITCH})
    detail, warn = _doctor_homing_how(adapter)
    assert "record for ender5 (read off this machine itself: firmware_family, z_travel_limit_mm, z_travel_limit_kind) says" in detail
    assert "asks for plate_clear first" in detail and warn is False


def test_the_doctor_says_when_a_backend_cannot_relay_the_reports(monkeypatch):
    """An Ender 5 behind OctoPrint: the same blanks, and no channel to ask the printer."""
    from kiln.cli.main import _doctor_homing_how

    from .test_filament_handling import _build

    adapter = _build("octoprint")
    adapter.set_safety_profile("ender5")
    detail, _warn = _doctor_homing_how(adapter)
    assert "record for ender5 says" in detail
    assert detail.endswith("Kiln has no reader for this backend's own firmware reports")
