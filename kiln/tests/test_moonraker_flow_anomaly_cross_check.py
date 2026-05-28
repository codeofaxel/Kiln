"""Coverage for the Moonraker → kiln-pro extrusion-event cross-check wire.

Klipper's ``print_stats`` exposes a ``state`` field and a ``message``
field over Moonraker's JSON-RPC ``notify_status_update`` push channel.
When ``state`` transitions to ``"error"`` with a message that names a
filament / extruder issue (filament_switch_sensor or
filament_motion_sensor runout, extruder shutdown), the Moonraker
adapter feeds that signal into kiln-pro's ``record_extrusion_event_for_printer``
so the wear cross-check can correlate flow signals against gram-count
wear estimates.

Tests cover:
- Classifier returns the right (event_type, severity) tuple per
  Klipper message substring.
- Non-error states and unrelated error messages return None — the
  wire skips them so generic firmware faults don't poison the wear
  signal.
- De-duplication: the same (state, message) fires once per session
  even when Klipper repeats the status push.
- The wire path falls through cleanly when kiln-pro is not installed
  (free tier).
"""

from __future__ import annotations

import sys

import pytest

from kiln.printers.moonraker import (
    _FLOW_ANOMALY_JAM_SUBSTRINGS,
    _FLOW_ANOMALY_UNDER_EXTRUSION_SUBSTRINGS,
    MoonrakerWebSocketMonitor,
    _classify_flow_anomaly,
)


class TestClassifyFlowAnomaly:
    def test_non_error_state_returns_none(self):
        assert _classify_flow_anomaly("printing", None) is None
        assert _classify_flow_anomaly("printing", "Filament Sensor: Runout") is None
        assert _classify_flow_anomaly("complete", "All good") is None
        assert _classify_flow_anomaly("paused", "Filament jam") is None

    def test_none_state_returns_none(self):
        assert _classify_flow_anomaly(None, "Filament Sensor runout") is None

    def test_error_without_message_returns_none(self):
        assert _classify_flow_anomaly("error", None) is None
        assert _classify_flow_anomaly("error", "") is None

    def test_error_with_unrelated_message_returns_none(self):
        # Generic Klipper shutdown — could be MCU loss, thermal runaway,
        # endstop fault.  None of these are flow signals, so the wire
        # MUST drop them to avoid poisoning the wear cross-check.
        assert _classify_flow_anomaly("error", "MCU 'mcu' shutdown") is None
        assert _classify_flow_anomaly("error", "Thermal runaway detected") is None
        assert _classify_flow_anomaly(
            "error", "Klipper has been shut down"
        ) is None

    def test_filament_sensor_runout_classifies_as_filament_jam_high(self):
        # Klipper's filament_switch_sensor emits this string on trip.
        result = _classify_flow_anomaly(
            "error",
            "Filament Sensor filament_runout: Runout detected",
        )
        assert result == ("filament_jam", "high")

    def test_filament_motion_sensor_classifies_as_filament_jam_high(self):
        # filament_motion_sensor uses similar wording.
        result = _classify_flow_anomaly(
            "error",
            "Filament Sensor encoder: Runout",
        )
        assert result == ("filament_jam", "high")

    def test_filament_runout_phrase_classifies_as_filament_jam(self):
        result = _classify_flow_anomaly("error", "Filament runout detected")
        assert result == ("filament_jam", "high")

    def test_filament_jam_phrase_classifies_as_filament_jam(self):
        result = _classify_flow_anomaly("error", "Filament jam in extruder")
        assert result == ("filament_jam", "high")

    def test_extruder_shutdown_classifies_as_under_extrusion_medium(self):
        result = _classify_flow_anomaly("error", "Extruder shutdown")
        assert result == ("under_extrusion", "medium")

    def test_extruder_not_ready_classifies_as_under_extrusion(self):
        result = _classify_flow_anomaly(
            "error",
            "Extruder not ready for extrude",
        )
        assert result == ("under_extrusion", "medium")

    def test_under_extrusion_phrase_classifies_as_under_extrusion(self):
        result = _classify_flow_anomaly("error", "Under extrusion detected")
        assert result == ("under_extrusion", "medium")

    def test_hyphenated_under_extrusion_classifies(self):
        result = _classify_flow_anomaly("error", "Under-extrusion warning")
        assert result == ("under_extrusion", "medium")

    def test_match_is_case_insensitive(self):
        # Klipper's message casing is consistent but defensive lowercasing
        # protects against custom KILN macros that build their own
        # PAUSE messages with different casing.
        result = _classify_flow_anomaly(
            "error",
            "FILAMENT SENSOR: RUNOUT DETECTED",
        )
        assert result == ("filament_jam", "high")

    def test_jam_takes_priority_over_under_extrusion(self):
        # If a message somehow names both — jam wins because it's the
        # higher-severity terminal signal.
        result = _classify_flow_anomaly(
            "error",
            "Filament Sensor: extruder shutdown after runout",
        )
        assert result == ("filament_jam", "high")


class TestFlowAnomalySubstringSet:
    def test_substring_lists_are_non_empty(self):
        # Pin the contract — losing either list silently disables the
        # wire on a whole class of signals.
        assert len(_FLOW_ANOMALY_JAM_SUBSTRINGS) > 0
        assert len(_FLOW_ANOMALY_UNDER_EXTRUSION_SUBSTRINGS) > 0

    def test_substrings_are_lowercase(self):
        # The classifier lowercases the incoming message; a non-lowercase
        # substring in the list would never match.
        for substring in _FLOW_ANOMALY_JAM_SUBSTRINGS:
            assert substring == substring.lower(), (
                f"jam substring {substring!r} must be lowercase"
            )
        for substring in _FLOW_ANOMALY_UNDER_EXTRUSION_SUBSTRINGS:
            assert substring == substring.lower(), (
                f"under-extrusion substring {substring!r} must be lowercase"
            )

    def test_jam_and_under_extrusion_disjoint(self):
        # No substring should appear in both lists — overlap would
        # depend on iteration order to disambiguate.
        jam_set = set(_FLOW_ANOMALY_JAM_SUBSTRINGS)
        under_set = set(_FLOW_ANOMALY_UNDER_EXTRUSION_SUBSTRINGS)
        assert jam_set.isdisjoint(under_set)


class TestMonitorDedup:
    """The monitor de-dupes (state, message) so a stuck error doesn't
    re-fire the cross-check on every status push."""

    def test_initial_dedup_key_is_none(self):
        monitor = MoonrakerWebSocketMonitor("http://klipper.local:7125")
        assert monitor._last_flow_anomaly_key is None

    def test_printer_name_threaded_through_constructor(self):
        monitor = MoonrakerWebSocketMonitor(
            "http://klipper.local:7125",
            printer_name="bench-klipper",
        )
        assert monitor._printer_name == "bench-klipper"

    def test_printer_name_defaults_to_moonraker(self):
        monitor = MoonrakerWebSocketMonitor("http://klipper.local:7125")
        assert monitor._printer_name == "moonraker"

    def test_first_anomaly_fires_then_repeat_is_skipped(self, monkeypatch):
        # Verify the wire only invokes record_extrusion_event_for_printer ONCE per
        # unique (state, message) pair.  This is the central de-dupe
        # contract — Klipper repeats the same status in every push
        # while the printer sits on an error.
        calls: list[dict] = []

        def _fake_record(**kwargs):
            calls.append(kwargs)

        # Install a fake kiln-pro module so the import inside the wire
        # resolves to our stub instead of ImportError-ing.
        import types

        fake_mod = types.ModuleType(
            "kiln_pro.nozzle_intelligence.sensor_signal"
        )
        fake_mod.record_extrusion_event_for_printer = _fake_record
        fake_parent = types.ModuleType("kiln_pro.nozzle_intelligence")
        fake_root = types.ModuleType("kiln_pro")
        monkeypatch.setitem(sys.modules, "kiln_pro", fake_root)
        monkeypatch.setitem(
            sys.modules, "kiln_pro.nozzle_intelligence", fake_parent,
        )
        monkeypatch.setitem(
            sys.modules,
            "kiln_pro.nozzle_intelligence.sensor_signal",
            fake_mod,
        )

        monitor = MoonrakerWebSocketMonitor(
            "http://klipper.local:7125",
            printer_name="klipper-test",
        )

        anomaly_status = {
            "print_stats": {
                "state": "error",
                "message": "Filament Sensor: Runout detected",
            },
        }

        monitor._maybe_record_flow_anomaly(anomaly_status)
        monitor._maybe_record_flow_anomaly(anomaly_status)
        monitor._maybe_record_flow_anomaly(anomaly_status)

        # All three pushes carry the same (state, message); only the
        # first fires.
        assert len(calls) == 1
        assert calls[0] == {
            "printer_id": "klipper-test",
            "event_type": "filament_jam",
            "severity": "high",
        }

    def test_recovery_clears_dedup_so_next_anomaly_fires(self, monkeypatch):
        calls: list[dict] = []

        def _fake_record(**kwargs):
            calls.append(kwargs)

        import types

        fake_mod = types.ModuleType(
            "kiln_pro.nozzle_intelligence.sensor_signal"
        )
        fake_mod.record_extrusion_event_for_printer = _fake_record
        fake_parent = types.ModuleType("kiln_pro.nozzle_intelligence")
        fake_root = types.ModuleType("kiln_pro")
        monkeypatch.setitem(sys.modules, "kiln_pro", fake_root)
        monkeypatch.setitem(
            sys.modules, "kiln_pro.nozzle_intelligence", fake_parent,
        )
        monkeypatch.setitem(
            sys.modules,
            "kiln_pro.nozzle_intelligence.sensor_signal",
            fake_mod,
        )

        monitor = MoonrakerWebSocketMonitor(
            "http://klipper.local:7125",
            printer_name="klipper-test",
        )

        # Fire once on filament jam.
        monitor._maybe_record_flow_anomaly({
            "print_stats": {
                "state": "error",
                "message": "Filament Sensor: Runout",
            },
        })
        # User resumes, state recovers to "printing".  This clears the
        # dedupe key so the next genuine anomaly is recorded.
        monitor._maybe_record_flow_anomaly({
            "print_stats": {"state": "printing", "message": ""},
        })
        # Second jam later in the print — should fire again.
        monitor._maybe_record_flow_anomaly({
            "print_stats": {
                "state": "error",
                "message": "Filament Sensor: Runout",
            },
        })

        assert len(calls) == 2


class TestImportErrorSafety:
    """When kiln-pro nozzle module is absent, the wire is silent."""

    def test_wire_swallows_import_error(self, monkeypatch):
        # Block the import of the kiln-pro sensor_signal module so the
        # wire takes the ImportError branch.  The monitor must NOT
        # raise — that would crash the WebSocket subscriber.
        monkeypatch.setitem(
            sys.modules,
            "kiln_pro.nozzle_intelligence.sensor_signal",
            None,
        )

        monitor = MoonrakerWebSocketMonitor(
            "http://klipper.local:7125",
            printer_name="klipper-test",
        )

        # This must not raise even though kiln-pro is unavailable.
        monitor._maybe_record_flow_anomaly({
            "print_stats": {
                "state": "error",
                "message": "Filament Sensor: Runout",
            },
        })

    def test_classifier_is_pure_no_import(self):
        # _classify_flow_anomaly must not touch kiln-pro at all — it's
        # a pure function callable from free-tier code paths.
        result = _classify_flow_anomaly(
            "error",
            "Filament Sensor: Runout",
        )
        # If this returned without raising, the function is import-free.
        assert result == ("filament_jam", "high")
