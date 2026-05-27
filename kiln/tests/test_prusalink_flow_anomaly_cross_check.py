"""Coverage for the Prusa Link → kiln-pro extrusion-event cross-check wire.

When Prusa Link's ``/api/v1/status`` reports a transition into an
ATTENTION / ERROR state whose message text implicates the filament
path (jam, runout, MMU error, blocked extruder), the PrusaConnect
adapter feeds that signal into kiln-pro's
``record_extrusion_event`` so the wear cross-check can correlate
flow signals against gram-count wear estimates.

Tests cover:
- Classifier returns the right ``(event_type, severity)`` tuple per
  state + message combination.
- Non-anomaly states (IDLE, BUSY, PRINTING, FINISHED, PAUSED,
  READY, STOPPED) return None — the wire skips them.
- Generic ATTENTION / ERROR without a filament-path message hint
  returns None — bed-leveling / user-pause / first-layer-calibration
  attentions don't poison the wear-rate signal.
- Transition-only firing — the adapter does NOT fire on steady-state
  ATTENTION across consecutive polls.
- The wire path falls through cleanly when kiln-pro is not
  installed (free tier).
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiln.printers.prusaconnect import (
    _ANOMALY_STATES,
    PrusaConnectAdapter,
    _classify_flow_anomaly,
)


# ---------------------------------------------------------------------------
# Helpers — synthesize a fake kiln-pro nozzle module so the wire's
# `from kiln_pro.nozzle_intelligence.sensor_signal import record_extrusion_event`
# resolves to a MagicMock during tests.  Without this, the wire's
# try/except ImportError catches every call site and the wire-firing
# assertions can't see the call.
# ---------------------------------------------------------------------------


def _install_fake_recorder(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Inject a fake kiln_pro.nozzle_intelligence.sensor_signal module.

    Returns a MagicMock standing in for ``record_extrusion_event``.
    Call assertions on the returned mock to verify the wire fired.
    """
    recorder = MagicMock()

    # Build the package chain.  Each parent must exist before the child
    # so ``from a.b.c import d`` walks the tree without ImportError.
    fake_nozzle = types.ModuleType("kiln_pro.nozzle_intelligence")
    fake_sensor = types.ModuleType("kiln_pro.nozzle_intelligence.sensor_signal")
    fake_sensor.record_extrusion_event = recorder
    fake_nozzle.sensor_signal = fake_sensor

    # If kiln_pro itself is importable, attach our fake nozzle module
    # to it; otherwise create a stub package too.
    try:
        import kiln_pro  # type: ignore
        monkeypatch.setattr(kiln_pro, "nozzle_intelligence", fake_nozzle, raising=False)
    except ImportError:
        fake_kp = types.ModuleType("kiln_pro")
        fake_kp.nozzle_intelligence = fake_nozzle
        monkeypatch.setitem(sys.modules, "kiln_pro", fake_kp)

    monkeypatch.setitem(sys.modules, "kiln_pro.nozzle_intelligence", fake_nozzle)
    monkeypatch.setitem(
        sys.modules, "kiln_pro.nozzle_intelligence.sensor_signal", fake_sensor
    )
    return recorder


# ---------------------------------------------------------------------------
# Classifier — pure function, no I/O
# ---------------------------------------------------------------------------


class TestClassifyFlowAnomaly:
    def test_empty_state_returns_none(self):
        assert _classify_flow_anomaly("") is None

    def test_idle_state_returns_none(self):
        assert _classify_flow_anomaly("IDLE") is None

    def test_busy_state_returns_none(self):
        assert _classify_flow_anomaly("BUSY") is None

    def test_printing_state_returns_none(self):
        assert _classify_flow_anomaly("PRINTING") is None

    def test_paused_state_returns_none(self):
        assert _classify_flow_anomaly("PAUSED") is None

    def test_finished_state_returns_none(self):
        assert _classify_flow_anomaly("FINISHED") is None

    def test_ready_state_returns_none(self):
        assert _classify_flow_anomaly("READY") is None

    def test_stopped_state_returns_none(self):
        assert _classify_flow_anomaly("STOPPED") is None

    def test_attention_without_message_returns_none(self):
        # Generic ATTENTION (door open, user-pause prompt) is too
        # ambiguous to attribute to flow — must have a filament hint.
        assert _classify_flow_anomaly("ATTENTION") is None
        assert _classify_flow_anomaly("ATTENTION", "") is None

    def test_error_without_filament_hint_returns_none(self):
        # ERROR is broader than ATTENTION and we deliberately do NOT
        # fire on it without explicit filament wording.
        assert _classify_flow_anomaly("ERROR") is None
        assert _classify_flow_anomaly("ERROR", "Thermal runaway protection") is None

    def test_attention_filament_runout_classifies_as_jam_high(self):
        result = _classify_flow_anomaly("ATTENTION", "Filament runout detected")
        assert result == ("filament_jam", "high")

    def test_attention_no_filament_classifies_as_jam_high(self):
        # MK3-era state flag form
        result = _classify_flow_anomaly("ATTENTION", "no_filament")
        assert result == ("filament_jam", "high")

    def test_attention_no_filament_human_form(self):
        result = _classify_flow_anomaly("ATTENTION", "No filament loaded")
        assert result == ("filament_jam", "high")

    def test_attention_jam_classifies_as_jam_high(self):
        result = _classify_flow_anomaly("ATTENTION", "Filament jam at extruder")
        assert result == ("filament_jam", "high")

    def test_attention_clog_classifies_as_jam_high(self):
        result = _classify_flow_anomaly("ATTENTION", "Nozzle clog suspected")
        assert result == ("filament_jam", "high")

    def test_mmu_error_classifies_as_jam_high(self):
        # MMU3 error states are virtually always filament-path.
        result = _classify_flow_anomaly("ATTENTION", "MMU error: filament not in finda")
        assert result == ("filament_jam", "high")

    def test_attention_blocked_extruder_classifies_as_under_extrusion(self):
        # Generic extruder warning without explicit jam/runout wording
        # falls into the medium under_extrusion bucket.
        result = _classify_flow_anomaly("ATTENTION", "Extruder fault")
        assert result == ("under_extrusion", "medium")

    def test_attention_feeding_abnormal_classifies_as_under_extrusion(self):
        result = _classify_flow_anomaly("ATTENTION", "Filament feeding abnormal")
        # "filament" in message → jam-suppressor check fails (no jam
        # word), but "feeding" / "filament" hint → under_extrusion.
        # Note: "filament" is in hints, but no jam-word, so under_extrusion.
        # (The presence of "filament" alone is the under_extrusion path.)
        assert result is not None
        event_type, severity = result
        assert event_type == "under_extrusion"
        assert severity == "medium"

    def test_error_with_filament_classifies_as_under_extrusion(self):
        # ERROR + filament word → medium under_extrusion (not high jam
        # unless the message also mentions runout/jam/no-filament/MMU).
        result = _classify_flow_anomaly("ERROR", "Filament path fault")
        assert result == ("under_extrusion", "medium")

    def test_error_with_jam_classifies_as_jam_high(self):
        # ERROR + explicit jam word → high jam.
        result = _classify_flow_anomaly("ERROR", "Critical jam: power off")
        assert result == ("filament_jam", "high")

    def test_attention_bed_leveling_suppressed(self):
        # ATTENTION during a calibration flow can mention "filament" in
        # an unrelated prompt — bed-leveling suppressor wins.
        result = _classify_flow_anomaly(
            "ATTENTION",
            "Bed leveling: insert filament when prompted",
        )
        assert result is None

    def test_attention_first_layer_suppressed(self):
        result = _classify_flow_anomaly(
            "ATTENTION",
            "First layer calibration in progress",
        )
        assert result is None

    def test_attention_user_pause_suppressed(self):
        result = _classify_flow_anomaly(
            "ATTENTION",
            "User pause requested at filament change",
        )
        assert result is None

    def test_case_insensitive_state(self):
        # Prusa Link emits upper-case but be forgiving.
        result = _classify_flow_anomaly("attention", "Filament runout")
        assert result == ("filament_jam", "high")

    def test_anomaly_states_contract(self):
        # Pin the contract — losing either state regresses the wire.
        assert "ATTENTION" in _ANOMALY_STATES
        assert "ERROR" in _ANOMALY_STATES


# ---------------------------------------------------------------------------
# Adapter wire — transition-only firing, kiln-pro hook
# ---------------------------------------------------------------------------


def _adapter() -> PrusaConnectAdapter:
    return PrusaConnectAdapter(host="http://prusa.local", api_key="t", retries=1)


def _status_payload(state: str, message: str | None = None) -> dict[str, Any]:
    printer: dict[str, Any] = {"state": state}
    if message is not None:
        printer["message"] = message
    return {"printer": printer}


class TestAdapterWireTransitions:
    """The wire fires only on transition INTO an anomaly state."""

    def test_fires_on_idle_to_attention_filament(self, monkeypatch):
        recorder = _install_fake_recorder(monkeypatch)
        adapter = _adapter()
        with patch.object(
            adapter,
            "_get_json",
            side_effect=[
                _status_payload("IDLE"),
                _status_payload("ATTENTION", "Filament runout"),
            ],
        ):
            adapter.get_state()
            adapter.get_state()

        assert recorder.called, "expected record_extrusion_event to fire on transition"
        kwargs = recorder.call_args.kwargs
        assert kwargs.get("event_type") == "filament_jam"
        assert kwargs.get("severity") == "high"
        assert kwargs.get("printer_id") == "prusaconnect"

    def test_does_not_fire_on_steady_state_attention(self, monkeypatch):
        recorder = _install_fake_recorder(monkeypatch)
        adapter = _adapter()
        with patch.object(
            adapter,
            "_get_json",
            side_effect=[
                _status_payload("ATTENTION", "Filament runout"),
                _status_payload("ATTENTION", "Filament runout"),
                _status_payload("ATTENTION", "Filament runout"),
            ],
        ):
            # First poll: prior=None → "ATTENTION".  This IS a
            # transition from "no observation" into anomaly and the
            # wire fires once.
            adapter.get_state()
            first_call_count = recorder.call_count

            # Second + third poll: steady-state — must NOT fire again.
            adapter.get_state()
            adapter.get_state()

        assert first_call_count == 1, "first poll should fire once"
        assert recorder.call_count == 1, (
            "steady-state ATTENTION must not re-fire — wire would flood "
            "the wear-signal log every poll cycle"
        )

    def test_does_not_fire_on_idle_to_printing(self, monkeypatch):
        recorder = _install_fake_recorder(monkeypatch)
        adapter = _adapter()
        with patch.object(
            adapter,
            "_get_json",
            side_effect=[
                _status_payload("IDLE"),
                _status_payload("PRINTING"),
            ],
        ):
            adapter.get_state()
            adapter.get_state()

        assert not recorder.called

    def test_does_not_fire_on_generic_attention_no_message(self, monkeypatch):
        recorder = _install_fake_recorder(monkeypatch)
        adapter = _adapter()
        with patch.object(
            adapter,
            "_get_json",
            side_effect=[
                _status_payload("IDLE"),
                _status_payload("ATTENTION"),  # no message → not flow
            ],
        ):
            adapter.get_state()
            adapter.get_state()

        assert not recorder.called

    def test_does_not_fire_on_attention_bed_leveling(self, monkeypatch):
        # ATTENTION mentioning "filament" inside a calibration prompt
        # is suppressed — bed-leveling / first-layer / user-pause text
        # is not a flow anomaly.
        recorder = _install_fake_recorder(monkeypatch)
        adapter = _adapter()
        with patch.object(
            adapter,
            "_get_json",
            side_effect=[
                _status_payload("IDLE"),
                _status_payload(
                    "ATTENTION",
                    "Bed leveling: insert filament when prompted",
                ),
            ],
        ):
            adapter.get_state()
            adapter.get_state()

        assert not recorder.called

    def test_fires_again_after_recovery_back_into_anomaly(self, monkeypatch):
        # IDLE → ATTENTION (fire) → IDLE → ATTENTION (fire again):
        # operator cleared the fault, machine returned to anomaly.
        # That's a NEW transition and should record a new event.
        recorder = _install_fake_recorder(monkeypatch)
        adapter = _adapter()
        with patch.object(
            adapter,
            "_get_json",
            side_effect=[
                _status_payload("IDLE"),
                _status_payload("ATTENTION", "Filament jam"),
                _status_payload("IDLE"),
                _status_payload("ATTENTION", "Filament jam"),
            ],
        ):
            adapter.get_state()
            adapter.get_state()
            adapter.get_state()
            adapter.get_state()

        assert recorder.call_count == 2

    def test_pulls_message_from_nested_error_dict(self, monkeypatch):
        # Prusa Link doesn't standardize the message surface — some
        # firmwares carry the text inside `printer.error.text`.
        recorder = _install_fake_recorder(monkeypatch)
        adapter = _adapter()
        payload = {
            "printer": {
                "state": "ATTENTION",
                "error": {"text": "Filament runout sensor triggered"},
            }
        }
        with patch.object(
            adapter,
            "_get_json",
            return_value=payload,
        ):
            adapter.get_state()

        assert recorder.called
        kwargs = recorder.call_args.kwargs
        assert kwargs.get("event_type") == "filament_jam"


# ---------------------------------------------------------------------------
# Free-tier safety
# ---------------------------------------------------------------------------


class TestImportErrorSafety:
    """When kiln-pro nozzle module is absent, the wire is silent."""

    def test_wire_swallows_import_error(self, monkeypatch):
        # Simulate kiln-pro nozzle module missing — assignment to None
        # makes `from kiln_pro... import ...` raise ImportError.
        monkeypatch.setitem(
            sys.modules,
            "kiln_pro.nozzle_intelligence.sensor_signal",
            None,
        )

        adapter = _adapter()
        with patch.object(
            adapter,
            "_get_json",
            side_effect=[
                _status_payload("IDLE"),
                _status_payload("ATTENTION", "Filament runout"),
            ],
        ):
            # The wire path is INSIDE get_state(); if it raises, the
            # status-poll happy path crashes.  Verify it doesn't.
            first = adapter.get_state()
            second = adapter.get_state()

        # Both polls returned a state object cleanly — free-tier
        # absence is silent.
        assert first is not None
        assert second is not None

    def test_import_path_exists_when_kiln_pro_installed_or_raises_cleanly(self):
        # Smoke check: importing the kiln-pro symbol either succeeds
        # (kiln-pro installed) or raises ImportError that the wire
        # catches.  Anything else (AttributeError, TypeError) would
        # bubble past the wire's `except ImportError`.
        try:
            from kiln_pro.nozzle_intelligence.sensor_signal import (  # noqa: F401
                record_extrusion_event,
            )
        except ImportError:
            pass  # expected on free-tier installs
        except Exception as exc:  # pragma: no cover
            pytest.fail(
                f"Unexpected exception during kiln-pro import: {exc!r}. "
                "The wire only catches ImportError; other exceptions "
                "would propagate into the status-poll path."
            )
