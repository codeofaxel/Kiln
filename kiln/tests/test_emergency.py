"""Tests for kiln.emergency -- emergency stop and safety interlock system.

Coverage areas:
- EmergencyCoordinator initialization
- emergency_stop() for a single printer (success and failure paths)
- emergency_stop_all() with multiple printers, mix of success/failure
- Safety interlock registration, update, and checking
- Critical interlock auto-triggering emergency stop when disengaged
- Stop clearing -- success and blocked by tripped interlocks
- History tracking (stop events recorded correctly)
- G-code sequence correctness
- Thread safety of concurrent operations
- Module-level convenience functions (get_emergency_coordinator, emergency_stop, emergency_stop_all)
- Dataclass to_dict() serialization
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional
from unittest import mock

import pytest

from kiln.emergency import (
    EmergencyCoordinator,
    EmergencyReason,
    EmergencyRecord,
    SafetyInterlock,
    _FDM_EMERGENCY_ACTIONS,
    _FDM_EMERGENCY_GCODE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_emergency_persistence(monkeypatch):
    """Keep tests isolated from any local persisted latch state."""
    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    monkeypatch.setenv("KILN_EMERGENCY_DEBOUNCE_SECONDS", "0")

@dataclass
class _FakeResult:
    """Minimal stand-in for PrintResult returned by adapter.emergency_stop()."""
    success: bool
    message: str = ""


class _FakeAdapter:
    """Minimal mock adapter with emergency_stop() and send_gcode()."""

    def __init__(
        self,
        *,
        estop_success: bool = True,
        estop_raises: bool = False,
        gcode_raises_on: Optional[set[str]] = None,
    ) -> None:
        self._estop_success = estop_success
        self._estop_raises = estop_raises
        self._gcode_raises_on = gcode_raises_on or set()
        self.estop_calls: int = 0
        self.gcode_calls: list[list[str]] = []

    def emergency_stop(self) -> _FakeResult:
        self.estop_calls += 1
        if self._estop_raises:
            raise RuntimeError("adapter offline")
        return _FakeResult(success=self._estop_success, message="ok" if self._estop_success else "fail")

    def send_gcode(self, commands: list[str]) -> bool:
        self.gcode_calls.append(commands)
        for cmd in commands:
            if cmd in self._gcode_raises_on:
                raise RuntimeError(f"gcode send failed: {cmd}")
        return True


def _make_registry(printers: dict[str, _FakeAdapter]):
    """Build a mock registry whose .get() and .list_names() work."""
    reg = mock.MagicMock()
    reg.list_names.return_value = sorted(printers.keys())

    def _get(name: str):
        if name not in printers:
            raise KeyError(name)
        return printers[name]

    reg.get.side_effect = _get
    return reg


# ---------------------------------------------------------------------------
# 1. EmergencyCoordinator initialization
# ---------------------------------------------------------------------------

class TestEmergencyCoordinatorInit:
    """Coordinator starts with empty state."""

    def test_no_stopped_printers(self):
        coord = EmergencyCoordinator()
        assert coord.is_stopped("any-printer") is False

    def test_empty_history(self):
        coord = EmergencyCoordinator()
        assert coord.get_stop_history() == []

    def test_no_interlocks(self):
        coord = EmergencyCoordinator()
        assert coord.check_interlocks("any-printer") == []


# ---------------------------------------------------------------------------
# 2. emergency_stop() single printer -- success and failure
# ---------------------------------------------------------------------------

class TestEmergencyStopSingle:
    """emergency_stop() for a single printer."""

    def test_success_via_hardware_estop(self):
        adapter = _FakeAdapter(estop_success=True)
        registry = _make_registry({"voron": adapter})

        coord = EmergencyCoordinator()
        with mock.patch("kiln.emergency.EmergencyCoordinator._send_emergency_gcode") as mock_send:
            mock_send.return_value = (list(_FDM_EMERGENCY_GCODE), list(_FDM_EMERGENCY_ACTIONS))
            record = coord.emergency_stop("voron", reason=EmergencyReason.THERMAL_RUNAWAY)

        assert record.success is True
        assert record.printer_id == "voron"
        assert record.reason == EmergencyReason.THERMAL_RUNAWAY
        assert record.error is None
        assert record.gcode_sent == list(_FDM_EMERGENCY_GCODE)
        assert record.actions_taken == list(_FDM_EMERGENCY_ACTIONS)

    def test_marks_printer_as_stopped(self):
        coord = EmergencyCoordinator()
        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            coord.emergency_stop("prusa")
        assert coord.is_stopped("prusa") is True

    def test_failure_still_marks_stopped(self):
        coord = EmergencyCoordinator()
        with mock.patch.object(coord, "_send_emergency_gcode", side_effect=RuntimeError("offline")):
            record = coord.emergency_stop("ender3")

        assert record.success is False
        assert record.error is not None
        assert "G-code delivery failed" in record.error
        assert coord.is_stopped("ender3") is True

    def test_failure_records_all_actions(self):
        coord = EmergencyCoordinator()
        with mock.patch.object(coord, "_send_emergency_gcode", side_effect=RuntimeError("timeout")):
            record = coord.emergency_stop("bambu")

        assert record.actions_taken == list(_FDM_EMERGENCY_ACTIONS)

    def test_default_reason_is_user_request(self):
        coord = EmergencyCoordinator()
        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            record = coord.emergency_stop("printer1")
        assert record.reason == EmergencyReason.USER_REQUEST

    def test_timestamp_is_set(self):
        coord = EmergencyCoordinator()
        before = time.time()
        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            record = coord.emergency_stop("printer1")
        after = time.time()
        assert before <= record.timestamp <= after

    def test_emit_event_called(self):
        coord = EmergencyCoordinator()
        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            with mock.patch.object(coord, "_emit_event") as mock_emit:
                coord.emergency_stop("voron", reason=EmergencyReason.COLLISION_DETECTED)

        mock_emit.assert_called_once_with(
            "voron",
            EmergencyReason.COLLISION_DETECTED,
            [],
        )


# ---------------------------------------------------------------------------
# 3. emergency_stop_all() -- multiple printers
# ---------------------------------------------------------------------------

class TestEmergencyStopAll:
    """emergency_stop_all() across the fleet."""

    def test_no_known_printers_returns_empty(self):
        coord = EmergencyCoordinator()
        with mock.patch("kiln.emergency.EmergencyCoordinator.emergency_stop_all") as orig:
            # Call the real method but patch the registry import to fail
            orig.side_effect = lambda **kw: []

        # Actually call the real implementation with no printers known
        coord2 = EmergencyCoordinator()
        with mock.patch.dict("sys.modules", {"kiln.server": None}):
            with mock.patch("kiln.emergency.EmergencyCoordinator.emergency_stop") as mock_stop:
                results = coord2.emergency_stop_all(reason=EmergencyReason.POWER_ANOMALY)
        assert results == []

    def test_stops_all_printers_from_interlocks(self):
        coord = EmergencyCoordinator()
        il = SafetyInterlock(
            name="enclosure",
            printer_id="voron",
            is_engaged=True,
            is_critical=False,
            last_checked=time.time(),
        )
        coord.register_interlock(il)

        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            # Patch the registry import to fail so only interlocks are the source
            with mock.patch.dict("sys.modules", {"kiln.server": mock.MagicMock(side_effect=ImportError)}):
                results = coord.emergency_stop_all(reason=EmergencyReason.POWER_ANOMALY)

        assert len(results) >= 1
        ids = [r.printer_id for r in results]
        assert "voron" in ids

    def test_stops_all_printers_from_registry(self):
        coord = EmergencyCoordinator()
        fake_registry = mock.MagicMock()
        fake_registry.list_names.return_value = ["ender3", "prusa"]

        fake_server = mock.MagicMock()
        fake_server._registry = fake_registry

        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            with mock.patch.dict("sys.modules", {"kiln.server": fake_server}):
                results = coord.emergency_stop_all()

        ids = [r.printer_id for r in results]
        assert "ender3" in ids
        assert "prusa" in ids

    def test_includes_previously_stopped_printers(self):
        coord = EmergencyCoordinator()
        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            coord.emergency_stop("old-printer")

        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            with mock.patch.dict("sys.modules", {"kiln.server": mock.MagicMock(side_effect=ImportError)}):
                results = coord.emergency_stop_all()

        ids = [r.printer_id for r in results]
        assert "old-printer" in ids

    def test_results_sorted_by_printer_id(self):
        coord = EmergencyCoordinator()
        for name in ["zebra", "alpha", "middle"]:
            il = SafetyInterlock(
                name="door",
                printer_id=name,
                is_engaged=True,
                is_critical=False,
                last_checked=time.time(),
            )
            coord.register_interlock(il)

        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            with mock.patch.dict("sys.modules", {"kiln.server": mock.MagicMock(side_effect=ImportError)}):
                results = coord.emergency_stop_all()

        ids = [r.printer_id for r in results]
        assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# 4. Interlock registration, update, and checking
# ---------------------------------------------------------------------------

class TestInterlockManagement:
    """Safety interlock registration, update, check."""

    def test_register_and_check(self):
        coord = EmergencyCoordinator()
        il = SafetyInterlock(
            name="enclosure_closed",
            printer_id="voron",
            is_engaged=True,
            is_critical=True,
            last_checked=time.time(),
        )
        coord.register_interlock(il)
        interlocks = coord.check_interlocks("voron")
        assert len(interlocks) == 1
        assert interlocks[0].name == "enclosure_closed"

    def test_register_multiple_interlocks(self):
        coord = EmergencyCoordinator()
        for name in ["enclosure", "filament_sensor", "bed_level"]:
            il = SafetyInterlock(
                name=name,
                printer_id="voron",
                is_engaged=True,
                is_critical=name == "enclosure",
                last_checked=time.time(),
            )
            coord.register_interlock(il)
        interlocks = coord.check_interlocks("voron")
        assert len(interlocks) == 3

    def test_check_interlocks_filters_by_printer(self):
        coord = EmergencyCoordinator()
        coord.register_interlock(SafetyInterlock("door", "voron", True, True, time.time()))
        coord.register_interlock(SafetyInterlock("door", "prusa", True, True, time.time()))
        assert len(coord.check_interlocks("voron")) == 1
        assert len(coord.check_interlocks("prusa")) == 1
        assert len(coord.check_interlocks("unknown")) == 0

    def test_register_overwrites_existing(self):
        coord = EmergencyCoordinator()
        il1 = SafetyInterlock("door", "voron", True, True, 1000.0)
        il2 = SafetyInterlock("door", "voron", False, False, 2000.0)
        coord.register_interlock(il1)
        coord.register_interlock(il2)

        interlocks = coord.check_interlocks("voron")
        assert len(interlocks) == 1
        assert interlocks[0].is_engaged is False
        assert interlocks[0].is_critical is False

    def test_update_interlock_changes_state(self):
        coord = EmergencyCoordinator()
        il = SafetyInterlock("door", "voron", True, False, time.time())
        coord.register_interlock(il)

        coord.update_interlock("voron", "door", is_engaged=False)
        interlocks = coord.check_interlocks("voron")
        assert interlocks[0].is_engaged is False

    def test_update_interlock_updates_timestamp(self):
        coord = EmergencyCoordinator()
        il = SafetyInterlock("door", "voron", True, False, 1000.0)
        coord.register_interlock(il)

        before = time.time()
        coord.update_interlock("voron", "door", is_engaged=True)
        after = time.time()

        interlocks = coord.check_interlocks("voron")
        assert before <= interlocks[0].last_checked <= after

    def test_update_unregistered_interlock_raises_key_error(self):
        coord = EmergencyCoordinator()
        with pytest.raises(KeyError, match="not registered"):
            coord.update_interlock("voron", "nonexistent", is_engaged=False)


# ---------------------------------------------------------------------------
# 5. Critical interlock auto-triggering emergency stop
# ---------------------------------------------------------------------------

class TestCriticalInterlockAutoStop:
    """Disengaging a critical interlock triggers emergency_stop."""

    def test_critical_disengage_triggers_estop(self):
        coord = EmergencyCoordinator()
        il = SafetyInterlock("enclosure", "voron", True, True, time.time())
        coord.register_interlock(il)

        with mock.patch.object(coord, "emergency_stop") as mock_estop:
            coord.update_interlock("voron", "enclosure", is_engaged=False)

        mock_estop.assert_called_once_with(
            "voron",
            reason=EmergencyReason.INTERLOCK_BREACH,
            source="interlock",
            note="critical interlock disengaged: enclosure",
        )

    def test_non_critical_disengage_does_not_trigger_estop(self):
        coord = EmergencyCoordinator()
        il = SafetyInterlock("filament_sensor", "voron", True, False, time.time())
        coord.register_interlock(il)

        with mock.patch.object(coord, "emergency_stop") as mock_estop:
            coord.update_interlock("voron", "filament_sensor", is_engaged=False)

        mock_estop.assert_not_called()

    def test_critical_re_engage_does_not_trigger_estop(self):
        coord = EmergencyCoordinator()
        il = SafetyInterlock("door", "voron", False, True, time.time())
        coord.register_interlock(il)

        with mock.patch.object(coord, "emergency_stop") as mock_estop:
            coord.update_interlock("voron", "door", is_engaged=True)

        mock_estop.assert_not_called()

    def test_critical_already_engaged_stays_engaged(self):
        coord = EmergencyCoordinator()
        il = SafetyInterlock("door", "voron", True, True, time.time())
        coord.register_interlock(il)

        with mock.patch.object(coord, "emergency_stop") as mock_estop:
            coord.update_interlock("voron", "door", is_engaged=True)

        mock_estop.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Stop clearing -- success and blocked by tripped interlocks
# ---------------------------------------------------------------------------

class TestClearStop:
    """clear_stop() behavior."""

    def test_clear_succeeds_when_all_critical_engaged(self):
        coord = EmergencyCoordinator()
        il = SafetyInterlock("door", "voron", True, True, time.time())
        coord.register_interlock(il)

        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            coord.emergency_stop("voron")
        assert coord.is_stopped("voron") is True

        result = coord.clear_stop("voron")
        assert result is True
        assert coord.is_stopped("voron") is False

    def test_clear_blocked_by_disengaged_critical_interlock(self):
        coord = EmergencyCoordinator()
        il = SafetyInterlock("door", "voron", False, True, time.time())
        coord.register_interlock(il)

        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            coord.emergency_stop("voron")

        result = coord.clear_stop("voron")
        assert result is False
        assert coord.is_stopped("voron") is True

    def test_clear_not_blocked_by_disengaged_non_critical_interlock(self):
        coord = EmergencyCoordinator()
        coord.register_interlock(SafetyInterlock("filament", "voron", False, False, time.time()))

        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            coord.emergency_stop("voron")

        result = coord.clear_stop("voron")
        assert result is True
        assert coord.is_stopped("voron") is False

    def test_clear_returns_false_when_not_stopped(self):
        coord = EmergencyCoordinator()
        result = coord.clear_stop("voron")
        assert result is False

    def test_clear_with_no_interlocks_succeeds(self):
        coord = EmergencyCoordinator()
        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            coord.emergency_stop("voron")

        result = coord.clear_stop("voron")
        assert result is True

    def test_clear_with_multiple_critical_all_engaged(self):
        coord = EmergencyCoordinator()
        coord.register_interlock(SafetyInterlock("door", "voron", True, True, time.time()))
        coord.register_interlock(SafetyInterlock("thermal", "voron", True, True, time.time()))

        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            coord.emergency_stop("voron")

        assert coord.clear_stop("voron") is True

    def test_clear_with_one_critical_disengaged_among_many(self):
        coord = EmergencyCoordinator()
        coord.register_interlock(SafetyInterlock("door", "voron", True, True, time.time()))
        coord.register_interlock(SafetyInterlock("thermal", "voron", False, True, time.time()))

        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            coord.emergency_stop("voron")

        assert coord.clear_stop("voron") is False


# ---------------------------------------------------------------------------
# 7. History tracking
# ---------------------------------------------------------------------------

class TestStopHistory:
    """get_stop_history() behavior."""

    def test_records_stop_events(self):
        coord = EmergencyCoordinator()
        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            coord.emergency_stop("printer-a", reason=EmergencyReason.THERMAL_RUNAWAY)
            coord.emergency_stop("printer-b", reason=EmergencyReason.MATERIAL_JAM)

        history = coord.get_stop_history()
        assert len(history) == 2

    def test_most_recent_first(self):
        coord = EmergencyCoordinator()
        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            coord.emergency_stop("first")
            coord.emergency_stop("second")
            coord.emergency_stop("third")

        history = coord.get_stop_history()
        assert history[0].printer_id == "third"
        assert history[-1].printer_id == "first"

    def test_filter_by_printer_id(self):
        coord = EmergencyCoordinator()
        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            coord.emergency_stop("voron")
            coord.emergency_stop("prusa")
            coord.emergency_stop("voron")

        history = coord.get_stop_history(printer_id="voron")
        assert len(history) == 2
        assert all(r.printer_id == "voron" for r in history)

    def test_limit_caps_results(self):
        coord = EmergencyCoordinator()
        with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
            for i in range(10):
                coord.emergency_stop(f"printer-{i}")

        history = coord.get_stop_history(limit=3)
        assert len(history) == 3

    def test_empty_history_for_unknown_printer(self):
        coord = EmergencyCoordinator()
        assert coord.get_stop_history(printer_id="ghost") == []

    def test_history_includes_failure_records(self):
        coord = EmergencyCoordinator()
        with mock.patch.object(coord, "_send_emergency_gcode", side_effect=RuntimeError("offline")):
            coord.emergency_stop("broken")

        history = coord.get_stop_history()
        assert len(history) == 1
        assert history[0].success is False
        assert history[0].error is not None


# ---------------------------------------------------------------------------
# 8. G-code sequence correctness
# ---------------------------------------------------------------------------

class TestGcodeSequence:
    """Verify the FDM emergency G-code and action lists."""

    def test_gcode_order(self):
        assert _FDM_EMERGENCY_GCODE == ["M112", "M104 S0", "M140 S0", "M84"]

    def test_action_labels_order(self):
        assert _FDM_EMERGENCY_ACTIONS == [
            "emergency_stop_m112",
            "hotend_heater_off",
            "bed_heater_off",
            "steppers_disabled",
        ]

    def test_gcode_and_actions_same_length(self):
        assert len(_FDM_EMERGENCY_GCODE) == len(_FDM_EMERGENCY_ACTIONS)


# ---------------------------------------------------------------------------
# 9. _send_emergency_gcode internal method
# ---------------------------------------------------------------------------

class TestSendEmergencyGcode:
    """_send_emergency_gcode() adapter interaction."""

    def test_hardware_estop_success(self):
        adapter = _FakeAdapter(estop_success=True)
        registry = _make_registry({"voron": adapter})

        coord = EmergencyCoordinator()
        with mock.patch("kiln.emergency.EmergencyCoordinator._get_registry", return_value=registry, create=True):
            # We need to patch the import inside _send_emergency_gcode
            pass

        # Directly test by patching the imports within the method
        fake_server = mock.MagicMock()
        fake_server._registry = registry

        with mock.patch.dict("sys.modules", {"kiln.server": fake_server}):
            gcode, actions = coord._send_emergency_gcode("voron")

        assert gcode == list(_FDM_EMERGENCY_GCODE)
        assert actions == list(_FDM_EMERGENCY_ACTIONS)
        assert adapter.estop_calls == 1

    def test_hardware_estop_failure_falls_back_to_gcode(self):
        adapter = _FakeAdapter(estop_success=False)
        registry = _make_registry({"voron": adapter})

        fake_server = mock.MagicMock()
        fake_server._registry = registry

        with mock.patch.dict("sys.modules", {"kiln.server": fake_server}):
            gcode, actions = coord_send_gcode_with_adapter(adapter, registry)

        assert adapter.estop_calls == 1
        assert len(adapter.gcode_calls) == 4  # one per command

    def test_hardware_estop_exception_falls_back_to_gcode(self):
        adapter = _FakeAdapter(estop_raises=True)
        registry = _make_registry({"voron": adapter})

        fake_server = mock.MagicMock()
        fake_server._registry = registry

        coord = EmergencyCoordinator()
        with mock.patch.dict("sys.modules", {"kiln.server": fake_server}):
            gcode, actions = coord._send_emergency_gcode("voron")

        assert adapter.estop_calls == 1
        assert len(adapter.gcode_calls) == 4

    def test_partial_gcode_failure_raises_printer_error(self):
        adapter = _FakeAdapter(estop_raises=True, gcode_raises_on={"M84"})
        registry = _make_registry({"voron": adapter})

        fake_server = mock.MagicMock()
        fake_server._registry = registry

        coord = EmergencyCoordinator()
        from kiln.printers.base import PrinterError

        with mock.patch.dict("sys.modules", {"kiln.server": fake_server}):
            with pytest.raises(PrinterError, match="Partial G-code delivery"):
                coord._send_emergency_gcode("voron")

        # Earlier commands were still attempted
        assert len(adapter.gcode_calls) == 4


# Helper for test that needs separate setup
def coord_send_gcode_with_adapter(adapter, registry):
    """Run _send_emergency_gcode with a patched registry."""
    fake_server = mock.MagicMock()
    fake_server._registry = registry

    coord = EmergencyCoordinator()
    with mock.patch.dict("sys.modules", {"kiln.server": fake_server}):
        return coord._send_emergency_gcode("voron")


# ---------------------------------------------------------------------------
# 10. Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety:
    """Concurrent operations do not corrupt state."""

    def test_concurrent_stops_all_recorded(self):
        coord = EmergencyCoordinator()
        num_threads = 20
        barrier = threading.Barrier(num_threads)
        errors: list[str] = []

        def _stop(printer_id: str) -> None:
            try:
                barrier.wait(timeout=5)
                with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
                    coord.emergency_stop(printer_id)
            except Exception as exc:
                errors.append(str(exc))

        threads = [
            threading.Thread(target=_stop, args=(f"printer-{i}",))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        history = coord.get_stop_history(limit=100)
        assert len(history) == num_threads

    def test_concurrent_interlock_updates(self):
        coord = EmergencyCoordinator()
        # Register non-critical interlocks to avoid triggering estop
        for i in range(10):
            coord.register_interlock(
                SafetyInterlock(f"sensor-{i}", "voron", True, False, time.time())
            )

        num_threads = 20
        barrier = threading.Barrier(num_threads)
        errors: list[str] = []

        def _update(idx: int) -> None:
            try:
                barrier.wait(timeout=5)
                name = f"sensor-{idx % 10}"
                coord.update_interlock("voron", name, is_engaged=(idx % 2 == 0))
            except Exception as exc:
                errors.append(str(exc))

        threads = [
            threading.Thread(target=_update, args=(i,))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []

    def test_concurrent_stop_and_clear(self):
        coord = EmergencyCoordinator()
        num_rounds = 20
        errors: list[str] = []

        def _stop_clear(idx: int) -> None:
            try:
                pid = f"printer-{idx}"
                with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])):
                    coord.emergency_stop(pid)
                coord.clear_stop(pid)
            except Exception as exc:
                errors.append(str(exc))

        threads = [
            threading.Thread(target=_stop_clear, args=(i,))
            for i in range(num_rounds)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []


# ---------------------------------------------------------------------------
# 11. Module-level convenience functions
# ---------------------------------------------------------------------------

class TestModuleLevelFunctions:
    """get_emergency_coordinator(), emergency_stop(), emergency_stop_all()."""

    def test_get_emergency_coordinator_returns_singleton(self):
        import kiln.emergency as mod

        # Reset the module-level singleton for a clean test
        original = mod._coordinator
        mod._coordinator = None
        try:
            c1 = mod.get_emergency_coordinator()
            c2 = mod.get_emergency_coordinator()
            assert c1 is c2
        finally:
            mod._coordinator = original

    def test_get_emergency_coordinator_creates_instance(self):
        import kiln.emergency as mod

        original = mod._coordinator
        mod._coordinator = None
        try:
            c = mod.get_emergency_coordinator()
            assert isinstance(c, EmergencyCoordinator)
        finally:
            mod._coordinator = original

    def test_module_emergency_stop_delegates(self):
        import kiln.emergency as mod

        mock_coord = mock.MagicMock(spec=EmergencyCoordinator)
        expected = EmergencyRecord(
            printer_id="voron",
            success=True,
            reason=EmergencyReason.USER_REQUEST,
            timestamp=time.time(),
        )
        mock_coord.emergency_stop.return_value = expected

        original = mod._coordinator
        mod._coordinator = mock_coord
        try:
            result = mod.emergency_stop("voron", reason=EmergencyReason.AGENT_REQUEST)
            mock_coord.emergency_stop.assert_called_once_with(
                "voron", reason=EmergencyReason.AGENT_REQUEST,
            )
        finally:
            mod._coordinator = original

    def test_module_emergency_stop_all_delegates(self):
        import kiln.emergency as mod

        mock_coord = mock.MagicMock(spec=EmergencyCoordinator)
        mock_coord.emergency_stop_all.return_value = []

        original = mod._coordinator
        mod._coordinator = mock_coord
        try:
            result = mod.emergency_stop_all(reason=EmergencyReason.SOFTWARE_FAULT)
            mock_coord.emergency_stop_all.assert_called_once_with(
                reason=EmergencyReason.SOFTWARE_FAULT,
            )
        finally:
            mod._coordinator = original


# ---------------------------------------------------------------------------
# 12. Dataclass serialization
# ---------------------------------------------------------------------------

class TestEmergencyRecordToDict:
    """EmergencyRecord.to_dict() serialization."""

    def test_to_dict_success(self):
        record = EmergencyRecord(
            printer_id="voron",
            success=True,
            reason=EmergencyReason.THERMAL_RUNAWAY,
            timestamp=1234567890.0,
            actions_taken=["emergency_stop_m112"],
            gcode_sent=["M112"],
            error=None,
        )
        d = record.to_dict()
        assert d["printer_id"] == "voron"
        assert d["success"] is True
        assert d["reason"] == "thermal_runaway"
        assert d["timestamp"] == 1234567890.0
        assert d["actions_taken"] == ["emergency_stop_m112"]
        assert d["gcode_sent"] == ["M112"]
        assert d["error"] is None

    def test_to_dict_failure(self):
        record = EmergencyRecord(
            printer_id="ender3",
            success=False,
            reason=EmergencyReason.POWER_ANOMALY,
            timestamp=1000.0,
            error="timeout",
        )
        d = record.to_dict()
        assert d["success"] is False
        assert d["error"] == "timeout"
        assert d["reason"] == "power_anomaly"

    def test_to_dict_enum_serialized_as_string(self):
        for reason in EmergencyReason:
            record = EmergencyRecord(
                printer_id="test",
                success=True,
                reason=reason,
                timestamp=0.0,
            )
            d = record.to_dict()
            assert d["reason"] == reason.value
            assert isinstance(d["reason"], str)


class TestSafetyInterlockToDict:
    """SafetyInterlock.to_dict() serialization."""

    def test_to_dict(self):
        il = SafetyInterlock(
            name="enclosure_closed",
            printer_id="voron",
            is_engaged=True,
            is_critical=True,
            last_checked=5000.0,
        )
        d = il.to_dict()
        assert d["name"] == "enclosure_closed"
        assert d["printer_id"] == "voron"
        assert d["is_engaged"] is True
        assert d["is_critical"] is True
        assert d["last_checked"] == 5000.0


# ---------------------------------------------------------------------------
# 13. EmergencyReason enum
# ---------------------------------------------------------------------------

class TestEmergencyReason:
    """EmergencyReason enum coverage."""

    def test_all_reasons_are_strings(self):
        for reason in EmergencyReason:
            assert isinstance(reason.value, str)

    def test_reason_count(self):
        assert len(EmergencyReason) == 8

    def test_reason_values_unique(self):
        values = [r.value for r in EmergencyReason]
        assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# 14. _emit_event best-effort behavior
# ---------------------------------------------------------------------------

class TestEmitEvent:
    """_emit_event() never raises."""

    def test_emit_event_does_not_raise_on_import_error(self):
        coord = EmergencyCoordinator()
        # If kiln.events cannot be imported, it should silently pass
        with mock.patch.dict("sys.modules", {"kiln.events": None}):
            # Should not raise
            coord._emit_event("voron", EmergencyReason.USER_REQUEST, [])

    def test_emit_event_does_not_raise_on_publish_exception(self):
        coord = EmergencyCoordinator()
        fake_bus = mock.MagicMock()
        fake_bus.publish.side_effect = RuntimeError("bus exploded")

        fake_events = mock.MagicMock()
        fake_events.EventType.SAFETY_ESCALATED = "safety_escalated"
        fake_events.Event.return_value = mock.MagicMock()
        fake_events.EventBus = mock.MagicMock

        fake_server = mock.MagicMock()
        fake_server._event_bus = fake_bus

        with mock.patch.dict("sys.modules", {
            "kiln.events": fake_events,
            "kiln.server": fake_server,
        }):
            # Should not raise even though bus.publish explodes
            coord._emit_event("voron", EmergencyReason.USER_REQUEST, [])
