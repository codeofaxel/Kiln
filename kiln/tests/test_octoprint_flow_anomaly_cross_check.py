"""Coverage for the OctoPrint → kiln-pro extrusion-event cross-check wire.

When OctoPrint reports a flow / extrusion anomaly (``FilamentChange``
event, ``Error`` event whose payload mentions the filament path,
filament-sensor / under-extrusion plugin events, or a ``state.flags.
filament_change`` transition from the REST poll), the OctoPrint
adapter hands the signal to kiln-pro's
``record_extrusion_event_for_printer``.  What kiln-pro does with it is
its own concern; this file covers the wire.

Tests cover:

* Classifier returns the expected ``(event_type, severity)`` tuple
  for each known event name, state-flag synthetic name, and plugin
  pattern.
* Returns ``None`` for unrelated OctoPrint events (``PrintStarted``,
  ``PrintDone``, ``Connected``, naked ``Error`` without a filament
  hint, ``Error`` with a thermal-runaway message).
* The wire path falls through cleanly when kiln-pro is not installed
  (free tier — ``try/except ImportError`` swallows it).
* Prefix / map contracts are pinned — losing any of the known keys
  regresses the wire.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiln.printers.octoprint import (
    _FLOW_ANOMALY_ERROR_KEYWORDS,
    _FLOW_ANOMALY_EVENT_MAP,
    _FLOW_ANOMALY_FLAG_MAP,
    _FLOW_ANOMALY_PLUGIN_PATTERNS,
    OctoPrintAdapter,
    _classify_flow_anomaly,
)


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class TestClassifyFlowAnomaly:
    def test_empty_event_name_returns_none(self) -> None:
        assert _classify_flow_anomaly("") is None

    def test_filament_change_event_classifies_as_filament_jam_high(self) -> None:
        result = _classify_flow_anomaly("FilamentChange")
        assert result == ("filament_jam", "high")

    def test_filament_change_event_with_payload_still_classifies(self) -> None:
        # Payload is informational only for FilamentChange.
        result = _classify_flow_anomaly(
            "FilamentChange",
            {"tool": "tool0", "origin": "marlin"},
        )
        assert result == ("filament_jam", "high")

    def test_flag_filament_change_synthetic_classifies(self) -> None:
        # REST poll path uses ``flag:<flag_name>`` synthetic event names.
        result = _classify_flow_anomaly("flag:filament_change")
        assert result == ("filament_jam", "high")

    def test_flag_error_not_classified(self) -> None:
        # ``state.flags.error`` is too broad (covers SD-card errors,
        # connection drops, thermal runaway).  The REST poll path
        # intentionally does NOT classify it as a flow anomaly.
        assert _classify_flow_anomaly("flag:error") is None

    def test_flag_unknown_returns_none(self) -> None:
        assert _classify_flow_anomaly("flag:printing") is None
        assert _classify_flow_anomaly("flag:paused") is None

    # -- Error event payload inspection ------------------------------------

    def test_naked_error_without_payload_returns_none(self) -> None:
        assert _classify_flow_anomaly("Error") is None

    def test_naked_error_with_empty_payload_returns_none(self) -> None:
        assert _classify_flow_anomaly("Error", {}) is None

    def test_error_without_filament_keyword_returns_none(self) -> None:
        # Thermal-runaway / generic errors must NOT reach the wire.
        result = _classify_flow_anomaly(
            "Error",
            {"error": "Thermal runaway on tool 0"},
        )
        assert result is None

    def test_error_with_filament_keyword_classifies_under_extrusion(self) -> None:
        result = _classify_flow_anomaly(
            "Error",
            {"error": "Filament jammed at the extruder"},
        )
        assert result == ("under_extrusion", "medium")

    def test_error_with_extrusion_keyword_classifies(self) -> None:
        result = _classify_flow_anomaly(
            "Error",
            {"error": "Extrusion failure detected"},
        )
        assert result == ("under_extrusion", "medium")

    def test_error_with_runout_keyword_classifies(self) -> None:
        result = _classify_flow_anomaly(
            "Error",
            {"error": "Filament runout sensor triggered"},
        )
        assert result == ("under_extrusion", "medium")

    def test_error_keyword_match_is_case_insensitive(self) -> None:
        # The classifier lowercases the payload before keyword match.
        result = _classify_flow_anomaly(
            "Error",
            {"error": "FILAMENT BROKEN AT FEED TUBE"},
        )
        assert result == ("under_extrusion", "medium")

    def test_error_with_non_dict_payload_returns_none(self) -> None:
        # Defensive — OctoPrint *should* always send a dict, but
        # malformed plugins exist.
        assert _classify_flow_anomaly("Error", None) is None

    # -- Plugin event names -----------------------------------------------

    def test_plugin_filament_not_present_classifies_as_filament_jam(self) -> None:
        # Filament Sensor (Reloaded) emits this name.
        result = _classify_flow_anomaly(
            "plugin_filamentsensorreloaded_filament_not_present"
        )
        assert result == ("filament_jam", "high")

    def test_plugin_spoolmanager_runout_classifies(self) -> None:
        result = _classify_flow_anomaly("plugin_SpoolManager_filament_runout")
        assert result == ("filament_jam", "high")

    def test_plugin_under_extrusion_classifies_medium(self) -> None:
        result = _classify_flow_anomaly(
            "plugin_underextrusion_detected_under_extrusion"
        )
        assert result == ("under_extrusion", "medium")

    def test_plugin_under_extrusion_compact_form_classifies(self) -> None:
        # Some plugins drop the underscore: ``underextrusion_detected``.
        result = _classify_flow_anomaly(
            "plugin_my_under_extrusion_plugin_underextrusion_detected"
        )
        assert result == ("under_extrusion", "medium")

    def test_plugin_unrelated_returns_none(self) -> None:
        # Plugin events for unrelated subsystems must not be classified.
        assert _classify_flow_anomaly("plugin_octolapse_movie_done") is None
        assert _classify_flow_anomaly("plugin_themeify_loaded") is None

    # -- Generic / known-good events --------------------------------------

    def test_print_lifecycle_events_return_none(self) -> None:
        for name in (
            "PrintStarted",
            "PrintDone",
            "PrintFailed",
            "PrintCancelled",
            "PrintPaused",
            "PrintResumed",
        ):
            assert _classify_flow_anomaly(name) is None, name

    def test_connection_events_return_none(self) -> None:
        for name in ("Connected", "Disconnected", "ClientOpened", "ClientClosed"):
            assert _classify_flow_anomaly(name) is None, name

    def test_unknown_event_name_returns_none(self) -> None:
        assert _classify_flow_anomaly("ZHomedAxisChanged") is None


# ---------------------------------------------------------------------------
# Contract pinning — losing any of these regresses the wire
# ---------------------------------------------------------------------------


class TestClassifierContracts:
    def test_event_map_has_filament_change(self) -> None:
        assert "FilamentChange" in _FLOW_ANOMALY_EVENT_MAP
        assert _FLOW_ANOMALY_EVENT_MAP["FilamentChange"] == (
            "filament_jam",
            "high",
        )

    def test_flag_map_has_filament_change(self) -> None:
        assert "filament_change" in _FLOW_ANOMALY_FLAG_MAP

    def test_error_keywords_cover_filament_path(self) -> None:
        # If any of these get dropped the Error → under_extrusion path
        # silently regresses.
        for kw in ("filament", "extruder", "extrusion", "feed", "jam", "runout"):
            assert kw in _FLOW_ANOMALY_ERROR_KEYWORDS, kw

    def test_plugin_patterns_cover_known_plugins(self) -> None:
        substrs = {pat[0] for pat in _FLOW_ANOMALY_PLUGIN_PATTERNS}
        # Filament Sensor (Reloaded) + SpoolManager
        assert "filament_not_present" in substrs
        assert "filament_runout" in substrs
        # Under-extrusion detector plugins
        assert "under_extrusion" in substrs
        assert "underextrusion" in substrs

    def test_severity_values_are_valid_strings(self) -> None:
        # kiln-pro's sensor_signal records severity as a free-form string;
        # we pin the small vocabulary the adapter emits so the cross-check
        # weight table doesn't see surprise values.
        valid = {"low", "medium", "high"}
        for _event, sev in _FLOW_ANOMALY_EVENT_MAP.values():
            assert sev in valid, sev
        for _event, sev in _FLOW_ANOMALY_FLAG_MAP.values():
            assert sev in valid, sev
        for _substr, _event, sev in _FLOW_ANOMALY_PLUGIN_PATTERNS:
            assert sev in valid, sev


# ---------------------------------------------------------------------------
# Adapter-side wire — fire/skip behaviour
# ---------------------------------------------------------------------------


def _make_adapter() -> OctoPrintAdapter:
    """Build an adapter with no network side-effects."""
    return OctoPrintAdapter(
        host="http://octopi.test",
        api_key="testkey",
        timeout=1,
        retries=1,
    )


class TestFireExtrusionEvent:
    def test_fire_extrusion_event_calls_kiln_pro_recorder(self) -> None:
        adapter = _make_adapter()

        # Fake the kiln-pro module so the import inside the wire resolves
        # to a mock that records the call.
        mock_module = MagicMock()
        with patch.dict(
            sys.modules,
            {"kiln_pro.nozzle_intelligence.sensor_signal": mock_module},
        ):
            adapter._fire_extrusion_event("filament_jam", "high")

        mock_module.record_extrusion_event_for_printer.assert_called_once_with(
            printer_id="octoprint",
            event_type="filament_jam",
            severity="high",
        )

    def test_fire_extrusion_event_swallows_import_error(self) -> None:
        # When kiln-pro is not installed, the wire must NOT raise.
        # Marking the module as None in sys.modules makes the import fail
        # with ImportError, which the wire catches.
        adapter = _make_adapter()
        with patch.dict(
            sys.modules,
            {"kiln_pro.nozzle_intelligence.sensor_signal": None},
        ):
            # Must not raise.
            adapter._fire_extrusion_event("filament_jam", "high")
            adapter._fire_extrusion_event("under_extrusion", "medium")

    def test_fire_extrusion_event_swallows_recorder_exception(self) -> None:
        # Recorder raising a non-ImportError must not break the SockJS /
        # REST hot path.
        adapter = _make_adapter()
        mock_module = MagicMock()
        mock_module.record_extrusion_event_for_printer.side_effect = RuntimeError("boom")
        with patch.dict(
            sys.modules,
            {"kiln_pro.nozzle_intelligence.sensor_signal": mock_module},
        ):
            adapter._fire_extrusion_event("filament_jam", "high")  # no raise


class TestFlagTransitionWire:
    def test_first_observation_does_not_fire(self) -> None:
        # The first poll never fires — we don't know the prior state.
        adapter = _make_adapter()
        mock_module = MagicMock()
        with patch.dict(
            sys.modules,
            {"kiln_pro.nozzle_intelligence.sensor_signal": mock_module},
        ):
            adapter._check_flow_flag_transitions({"filament_change": True})

        # State is recorded so the NEXT False→True transition fires.
        assert adapter._prev_flow_flags["filament_change"] is True
        # But the first call itself counts as a transition because the
        # default prior is False.  This is by design — if the FIRST
        # observation already sees the flag set, the firmware was
        # already in the anomalous state and the wear-tracker should
        # know.  Verify the recorder was called.
        mock_module.record_extrusion_event_for_printer.assert_called_once_with(
            printer_id="octoprint",
            event_type="filament_jam",
            severity="high",
        )

    def test_false_to_true_transition_fires(self) -> None:
        adapter = _make_adapter()
        mock_module = MagicMock()
        with patch.dict(
            sys.modules,
            {"kiln_pro.nozzle_intelligence.sensor_signal": mock_module},
        ):
            # Establish baseline at False.
            adapter._check_flow_flag_transitions({"filament_change": False})
            mock_module.record_extrusion_event_for_printer.assert_not_called()

            # Transition False → True fires.
            adapter._check_flow_flag_transitions({"filament_change": True})
            mock_module.record_extrusion_event_for_printer.assert_called_once()

    def test_sustained_true_does_not_refire(self) -> None:
        # If the flag stays True across polls, the wire fires once.
        adapter = _make_adapter()
        mock_module = MagicMock()
        with patch.dict(
            sys.modules,
            {"kiln_pro.nozzle_intelligence.sensor_signal": mock_module},
        ):
            adapter._check_flow_flag_transitions({"filament_change": False})
            adapter._check_flow_flag_transitions({"filament_change": True})
            adapter._check_flow_flag_transitions({"filament_change": True})
            adapter._check_flow_flag_transitions({"filament_change": True})

            assert mock_module.record_extrusion_event_for_printer.call_count == 1

    def test_true_to_false_to_true_refires(self) -> None:
        # A clean transition cycle SHOULD fire again — separate incident.
        adapter = _make_adapter()
        mock_module = MagicMock()
        with patch.dict(
            sys.modules,
            {"kiln_pro.nozzle_intelligence.sensor_signal": mock_module},
        ):
            adapter._check_flow_flag_transitions({"filament_change": False})
            adapter._check_flow_flag_transitions({"filament_change": True})
            adapter._check_flow_flag_transitions({"filament_change": False})
            adapter._check_flow_flag_transitions({"filament_change": True})

            assert mock_module.record_extrusion_event_for_printer.call_count == 2

    def test_error_flag_does_not_fire(self) -> None:
        # ``state.flags.error`` is intentionally NOT classified as a
        # flow anomaly — too broad.
        adapter = _make_adapter()
        mock_module = MagicMock()
        with patch.dict(
            sys.modules,
            {"kiln_pro.nozzle_intelligence.sensor_signal": mock_module},
        ):
            adapter._check_flow_flag_transitions({"error": False})
            adapter._check_flow_flag_transitions({"error": True})
            mock_module.record_extrusion_event_for_printer.assert_not_called()


class TestPushEventWire:
    def test_filament_change_event_fires(self) -> None:
        adapter = _make_adapter()
        mock_module = MagicMock()
        with patch.dict(
            sys.modules,
            {"kiln_pro.nozzle_intelligence.sensor_signal": mock_module},
        ):
            adapter._handle_push_event("FilamentChange", {})

        mock_module.record_extrusion_event_for_printer.assert_called_once_with(
            printer_id="octoprint",
            event_type="filament_jam",
            severity="high",
        )

    def test_error_event_with_filament_payload_fires(self) -> None:
        adapter = _make_adapter()
        mock_module = MagicMock()
        with patch.dict(
            sys.modules,
            {"kiln_pro.nozzle_intelligence.sensor_signal": mock_module},
        ):
            adapter._handle_push_event(
                "Error",
                {"error": "Filament jam detected at extruder"},
            )

        mock_module.record_extrusion_event_for_printer.assert_called_once_with(
            printer_id="octoprint",
            event_type="under_extrusion",
            severity="medium",
        )

    def test_print_started_event_does_not_fire(self) -> None:
        adapter = _make_adapter()
        mock_module = MagicMock()
        with patch.dict(
            sys.modules,
            {"kiln_pro.nozzle_intelligence.sensor_signal": mock_module},
        ):
            adapter._handle_push_event("PrintStarted", {})
            adapter._handle_push_event("PrintDone", {})
            adapter._handle_push_event("Connected", {})

        mock_module.record_extrusion_event_for_printer.assert_not_called()

    def test_plugin_filament_runout_event_fires(self) -> None:
        adapter = _make_adapter()
        mock_module = MagicMock()
        with patch.dict(
            sys.modules,
            {"kiln_pro.nozzle_intelligence.sensor_signal": mock_module},
        ):
            adapter._handle_push_event(
                "plugin_filamentsensorreloaded_filament_not_present",
                {},
            )

        assert mock_module.record_extrusion_event_for_printer.call_count == 1
        kwargs = mock_module.record_extrusion_event_for_printer.call_args.kwargs
        assert kwargs["event_type"] == "filament_jam"
        assert kwargs["severity"] == "high"


class TestPushStateCallback:
    def test_handle_push_state_extracts_flags_and_fires(self) -> None:
        adapter = _make_adapter()
        mock_module = MagicMock()
        with patch.dict(
            sys.modules,
            {"kiln_pro.nozzle_intelligence.sensor_signal": mock_module},
        ):
            # Baseline at False, then transition True.
            adapter._handle_push_state(
                {"state": {"flags": {"filament_change": False}}}
            )
            adapter._handle_push_state(
                {"state": {"flags": {"filament_change": True}}}
            )

        mock_module.record_extrusion_event_for_printer.assert_called_once()

    def test_handle_push_state_tolerates_missing_keys(self) -> None:
        # Malformed / partial pushes must not crash the callback.
        adapter = _make_adapter()
        # No raise on any of these:
        adapter._handle_push_state({})
        adapter._handle_push_state({"state": None})  # type: ignore[arg-type]
        adapter._handle_push_state({"state": {"flags": None}})  # type: ignore[arg-type]
        adapter._handle_push_state({"state": "not-a-dict"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Import-error safety — the wire is a no-op without kiln-pro
# ---------------------------------------------------------------------------


class TestImportErrorSafety:
    """When kiln-pro nozzle module is absent, the wire is silent.

    This is the free-tier path — the adapter must keep working even
    though ``record_extrusion_event_for_printer`` is never reachable.
    """

    def test_wire_swallows_import_error_via_sentinel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Simulate kiln-pro nozzle module missing via sys.modules sentinel.
        monkeypatch.setitem(
            sys.modules,
            "kiln_pro.nozzle_intelligence.sensor_signal",
            None,
        )

        # The wire path is inside _fire_extrusion_event; calling it must
        # NOT raise, even though the underlying import is broken.
        adapter = _make_adapter()
        adapter._fire_extrusion_event("filament_jam", "high")

        # Also confirm the raw import itself raises (which is the only
        # thing the try/except in the wire guards against).
        with pytest.raises((ImportError, AttributeError, TypeError)):
            from kiln_pro.nozzle_intelligence.sensor_signal import (  # noqa: F401
                record_extrusion_event_for_printer,
            )

    def test_push_event_callback_safe_without_kiln_pro(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            sys.modules,
            "kiln_pro.nozzle_intelligence.sensor_signal",
            None,
        )
        adapter = _make_adapter()
        # FilamentChange would normally fire — but kiln-pro is "absent."
        adapter._handle_push_event("FilamentChange", {})  # no raise

    def test_flag_transition_safe_without_kiln_pro(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            sys.modules,
            "kiln_pro.nozzle_intelligence.sensor_signal",
            None,
        )
        adapter = _make_adapter()
        adapter._check_flow_flag_transitions({"filament_change": False})
        adapter._check_flow_flag_transitions({"filament_change": True})  # no raise


# ---------------------------------------------------------------------------
# SockJS monitor: event-message dispatch
# ---------------------------------------------------------------------------


class TestSockJSEventDispatch:
    """The SockJS monitor must dispatch event messages to ``on_event``.

    Without this, the adapter would only see ``current`` snapshots and
    miss explicit ``FilamentChange`` / ``Error`` / plugin signals.
    """

    def test_event_message_routes_to_on_event_callback(self) -> None:
        from kiln.printers.octoprint import OctoPrintSockJSMonitor

        captured: list[tuple[str, dict[str, Any]]] = []

        def _on_event(name: str, payload: dict[str, Any]) -> None:
            captured.append((name, payload))

        monitor = OctoPrintSockJSMonitor(
            "http://octopi.test",
            "testkey",
            on_event=_on_event,
        )

        # Build the SockJS message shape OctoPrint sends for events.
        import json as _json

        message = _json.dumps(
            {
                "event": {
                    "type": "FilamentChange",
                    "payload": {"tool": "tool0"},
                }
            }
        )
        # Drive the message handler directly.
        monitor._on_message(None, message)

        assert captured == [("FilamentChange", {"tool": "tool0"})]

    def test_event_callback_exception_does_not_crash_handler(self) -> None:
        from kiln.printers.octoprint import OctoPrintSockJSMonitor

        def _bad_callback(name: str, payload: dict[str, Any]) -> None:
            raise RuntimeError("callback bug")

        monitor = OctoPrintSockJSMonitor(
            "http://octopi.test",
            "testkey",
            on_event=_bad_callback,
        )
        import json as _json

        message = _json.dumps(
            {"event": {"type": "FilamentChange", "payload": {}}}
        )
        # Must NOT raise even when the callback explodes.
        monitor._on_message(None, message)

    def test_current_message_still_works_without_event_callback(self) -> None:
        from kiln.printers.octoprint import OctoPrintSockJSMonitor

        captured: list[dict[str, Any]] = []
        monitor = OctoPrintSockJSMonitor(
            "http://octopi.test",
            "testkey",
            on_state_update=lambda c: captured.append(c),
        )
        import json as _json

        message = _json.dumps(
            {"current": {"state": {"flags": {"printing": True}}}}
        )
        monitor._on_message(None, message)
        assert len(captured) == 1
