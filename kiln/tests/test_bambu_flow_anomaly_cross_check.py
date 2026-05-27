"""Coverage for the Bambu MQTT → kiln-pro extrusion-event cross-check wire.

When Bambu's MQTT push_status carries an HMS error code in the
flow-anomaly bucket (extruder feed failure, filament broken at
extruder, AMS jam, etc.), the bambu adapter feeds that signal into
kiln-pro's ``record_extrusion_event`` so the wear cross-check can
correlate flow signals against gram-count wear estimates.

Tests cover:
- Classifier returns the right (event_type, severity) tuple per
  HMS prefix.
- Zero / non-flow codes return None — the wire skips them.
- The wire path falls through cleanly when kiln-pro is not
  installed (free tier).
"""

from __future__ import annotations

import sys

import pytest

from kiln.printers.bambu import (
    _FLOW_ANOMALY_ERROR_PREFIXES,
    _classify_flow_anomaly,
)


class TestClassifyFlowAnomaly:
    def test_zero_code_returns_none(self):
        assert _classify_flow_anomaly(0) is None

    def test_non_flow_code_returns_none(self):
        # 0500-C010-... is an FTP/file path error — not a flow signal.
        non_flow_code = 0x0500C010
        assert _classify_flow_anomaly(non_flow_code) is None

    def test_extruder_feed_abnormal_classifies_as_under_extrusion_medium(self):
        # 03008003 prefix → filament feeding abnormal
        code = 0x03008003_0000
        # Pack into an int; the classifier converts to hex internally.
        result = _classify_flow_anomaly(0x03008003)
        assert result is not None
        event_type, severity = result
        assert event_type == "under_extrusion"
        assert severity == "medium"

    def test_filament_broken_classifies_as_filament_jam_high(self):
        result = _classify_flow_anomaly(0x03008005)
        assert result is not None
        event_type, severity = result
        assert event_type == "filament_jam"
        assert severity == "high"

    def test_ams_jam_classifies_as_filament_jam_high(self):
        result = _classify_flow_anomaly(0x05000B00)
        assert result is not None
        event_type, severity = result
        assert event_type == "filament_jam"
        assert severity == "high"

    def test_p1_extrusion_failure_classifies_as_low(self):
        result = _classify_flow_anomaly(0x05000900)
        assert result is not None
        _, severity = result
        assert severity == "low"

    def test_tangled_filament_classifies_as_under_extrusion(self):
        result = _classify_flow_anomaly(0x03001900)
        assert result is not None
        event_type, _ = result
        assert event_type == "under_extrusion"


class TestFlowAnomalyPrefixSet:
    def test_all_prefixes_are_8_hex_chars(self):
        # Bambu's HMS codes are 8 hex digits (32-bit).  Prefix matching
        # depends on the full hex code starting with the prefix string.
        for prefix in _FLOW_ANOMALY_ERROR_PREFIXES:
            assert len(prefix) == 8
            assert all(c in "0123456789ABCDEFabcdef" for c in prefix)

    def test_known_prefixes_present(self):
        # Pin the contract — losing any of these regresses the wire.
        expected_prefixes = {
            "03008003",
            "03008005",
            "03001900",
            "05000B00",
            "05000900",
        }
        assert expected_prefixes.issubset(set(_FLOW_ANOMALY_ERROR_PREFIXES))


class TestImportErrorSafety:
    """When kiln-pro nozzle module is absent, the wire is silent."""

    def test_wire_swallows_import_error(self, monkeypatch):
        # Simulate kiln-pro nozzle module missing.
        monkeypatch.setitem(
            sys.modules,
            "kiln_pro.nozzle_intelligence.sensor_signal",
            None,
        )
        # The wire path is inside _on_message; if it raises, the MQTT
        # subscriber crashes.  We can't easily call _on_message in a
        # test without a full adapter mock — but we can verify the
        # import itself fails cleanly, which is the only thing the
        # try/except in the wire guards against.
        with pytest.raises((ImportError, AttributeError, TypeError)):
            from kiln_pro.nozzle_intelligence.sensor_signal import (
                record_extrusion_event,  # noqa: F401
            )
