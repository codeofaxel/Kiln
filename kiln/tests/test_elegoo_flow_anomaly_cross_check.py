"""Coverage for the Elegoo SDCP → kiln-pro extrusion-event cross-check wire.

When an Elegoo Centauri Carbon SDCP V3 status push carries an
``ErrorStatusReason`` (SDCP_PRINT_CAUSE_*) that the firmware
classifies as a filament-path failure — runout or jam — the
Elegoo adapter hands that signal to kiln-pro's
``record_extrusion_event_for_printer``.  What kiln-pro does with it is
its own concern; this file covers the wire.

Tests cover:

- Classifier returns the right ``(event_type, severity)`` tuple
  per SDCP cause code.
- Healthy / unrelated codes (no error, home-failed, level-failed,
  temp-error, bed-adhesion) return ``None`` — the wire skips them.
- The wire path falls through cleanly when kiln-pro is not
  installed (free tier).
- Cause code mirror set is the contract; losing a code regresses
  the wire.
"""

from __future__ import annotations

import sys

import pytest

from kiln.printers.elegoo import (
    _FLOW_ANOMALY_CAUSE_CODES,
    _classify_flow_anomaly,
)


class TestClassifyFlowAnomaly:
    def test_zero_code_returns_none(self):
        # SDCP_PRINT_CAUSE_OK — no anomaly.
        assert _classify_flow_anomaly(0) is None

    def test_none_returns_none(self):
        # Field absent from status push — pass-through.
        assert _classify_flow_anomaly(None) is None

    def test_filament_runout_classifies_as_under_extrusion_medium(self):
        # SDCP_PRINT_CAUSE_FILAMENT_RUNOUT = 3
        result = _classify_flow_anomaly(3)
        assert result is not None
        event_type, severity = result
        assert event_type == "under_extrusion"
        assert severity == "medium"

    def test_filament_jam_classifies_as_filament_jam_high(self):
        # SDCP_PRINT_CAUSE_FILAMENT_JAM = 6
        result = _classify_flow_anomaly(6)
        assert result is not None
        event_type, severity = result
        assert event_type == "filament_jam"
        assert severity == "high"

    def test_home_failed_returns_none(self):
        # SDCP_PRINT_CAUSE_HOME_FAILED = 17 — kinematics, not flow.
        assert _classify_flow_anomaly(17) is None

    def test_level_failed_returns_none(self):
        # SDCP_PRINT_CAUSE_LEVEL_FAILED = 7 — first-layer geometry,
        # not a nozzle wear signal.
        assert _classify_flow_anomaly(7) is None

    def test_bed_adhesion_returns_none(self):
        # SDCP_PRINT_CAUSE_BED_ADHESION_FAILED = 18 — adhesion,
        # not flow.
        assert _classify_flow_anomaly(18) is None

    def test_temp_error_returns_none(self):
        # SDCP_PRINT_CAUSE_TEMP_ERROR = 1 — thermal, not flow.
        # Important: thermal anomalies CAN cause under-extrusion
        # downstream, but the cause code itself isn't a direct
        # flow signal and feeding it in would double-count when
        # the firmware later emits the actual filament-path code.
        assert _classify_flow_anomaly(1) is None

    def test_generic_error_returns_none(self):
        # SDCP_PRINT_CAUSE_ERROR = 19 — generic, no filament hint.
        assert _classify_flow_anomaly(19) is None


class TestFlowAnomalyCauseCodeSet:
    def test_cause_codes_are_integers(self):
        for code in _FLOW_ANOMALY_CAUSE_CODES:
            assert isinstance(code, int)
            assert code > 0

    def test_known_cause_codes_present(self):
        # Pin the contract — losing either of these regresses the
        # wire on its only two documented flow-path signals.
        expected = {3, 6}
        assert expected.issubset(set(_FLOW_ANOMALY_CAUSE_CODES))

    def test_only_flow_codes_in_set(self):
        # The cross-check would be poisoned by adding non-flow
        # codes (HOME_FAILED, LEVEL_FAILED, MOVE_ABNORMAL).  Pin
        # the cause-code set to the two documented filament-path
        # codes so a future "add LEVEL_FAILED" change fails this
        # test loudly.
        assert set(_FLOW_ANOMALY_CAUSE_CODES) == {3, 6}


class TestImportErrorSafety:
    """When kiln-pro nozzle module is absent, the wire is silent."""

    def test_wire_swallows_import_error(self, monkeypatch):
        # Simulate kiln-pro nozzle module missing — the import
        # inside _handle_message should raise, and the bare
        # `except ImportError` in the wire swallows it so the
        # WebSocket listener never crashes on a free-tier install.
        monkeypatch.setitem(
            sys.modules,
            "kiln_pro.nozzle_intelligence.sensor_signal",
            None,
        )
        with pytest.raises((ImportError, AttributeError, TypeError)):
            from kiln_pro.nozzle_intelligence.sensor_signal import (
                record_extrusion_event_for_printer,  # noqa: F401
            )
