"""An emergency stop also halts active AMS drying, scope-matched to the
printer stop.

Belt-and-suspenders safety: the per-unit temperature cap already prevents an
overheat; this ensures a true emergency darkens the dryer too.  A single-
printer E-stop halts only that printer's dryers; a fleet E-stop halts all;
a routine cancel (which never goes through EmergencyCoordinator) leaves the
dryer running.
"""

from __future__ import annotations

from unittest import mock

import pytest

from kiln.emergency import EmergencyCoordinator, EmergencyReason


@pytest.fixture(autouse=True)
def _no_persist_no_debounce(monkeypatch):
    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    monkeypatch.setenv("KILN_EMERGENCY_DEBOUNCE_SECONDS", "0")


class _FakeBambuAdapter:
    """Records the drying commands it is asked to publish."""

    def __init__(self, units):
        self._units = list(units)
        self.drying_commands: list[tuple[str, dict]] = []

    def get_ams_status(self):
        return {"units": [{"unit_id": u} for u in self._units]}

    def publish_print_command(self, command, params=None):
        self.drying_commands.append((command, params))
        return True


class _FakeNonBambuAdapter:
    """No AMS / drying surface at all (e.g. an OctoPrint printer)."""


def _registry_with(printers: dict):
    reg = mock.MagicMock()
    reg.list_names.return_value = sorted(printers)

    def _get(name):
        if name not in printers:
            raise KeyError(name)
        return printers[name]

    reg.get.side_effect = _get
    return reg


def _estop(coord, printers, action):
    """Run an E-stop with gcode bypassed and the registry pointed at fakes."""
    with mock.patch.object(coord, "_send_emergency_gcode", return_value=([], [])), \
         mock.patch("kiln.server._registry", _registry_with(printers)):
        return action(coord)


class TestEmergencyHaltsAmsDrying:
    def test_per_printer_estop_halts_that_dryer(self):
        a = _FakeBambuAdapter(units=[0, 1])
        coord = EmergencyCoordinator()
        _estop(coord, {"bambu-a": a}, lambda c: c.emergency_stop("bambu-a"))
        assert [cmd for cmd, _ in a.drying_commands] == ["ams_filament_drying"] * 2
        assert [p["mode"] for _, p in a.drying_commands] == [0, 0]  # STOP
        assert [p["ams_id"] for _, p in a.drying_commands] == [0, 1]

    def test_scope_matched_other_printers_keep_drying(self):
        a = _FakeBambuAdapter(units=[0])
        b = _FakeBambuAdapter(units=[0])
        coord = EmergencyCoordinator()
        _estop(coord, {"a": a, "b": b}, lambda c: c.emergency_stop("a"))
        assert len(a.drying_commands) == 1  # the stopped printer's dryer halts
        assert b.drying_commands == []      # the rest keep printing AND drying

    def test_fleet_estop_halts_every_dryer(self):
        a = _FakeBambuAdapter(units=[0])
        b = _FakeBambuAdapter(units=[0, 1])
        coord = EmergencyCoordinator()
        _estop(coord, {"a": a, "b": b}, lambda c: c.emergency_stop_all())
        assert len(a.drying_commands) == 1
        assert len(b.drying_commands) == 2

    def test_non_bambu_adapter_is_a_safe_noop(self):
        coord = EmergencyCoordinator()
        rec = _estop(
            coord, {"octo": _FakeNonBambuAdapter()},
            lambda c: c.emergency_stop("octo", reason=EmergencyReason.USER_REQUEST),
        )
        assert rec.success is True  # no dryer, no error

    def test_dryer_halt_failure_never_blocks_the_estop(self):
        class _Boom(_FakeBambuAdapter):
            def get_ams_status(self):
                raise RuntimeError("mqtt down")

        coord = EmergencyCoordinator()
        rec = _estop(coord, {"p": _Boom(units=[0])}, lambda c: c.emergency_stop("p"))
        assert rec.success is True  # the printer E-stop is still recorded
