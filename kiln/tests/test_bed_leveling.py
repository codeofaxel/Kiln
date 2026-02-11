"""Tests for kiln.bed_leveling — bed leveling trigger system."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from kiln.bed_leveling import BedLevelManager, LevelingPolicy, LevelingStatus
from kiln.persistence import KilnDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    return KilnDB(str(tmp_path / "test.db"))


@pytest.fixture
def bus():
    return MagicMock()


@pytest.fixture
def mgr(db, bus):
    return BedLevelManager(db=db, event_bus=bus)


@pytest.fixture
def mock_adapter():
    adapter = MagicMock()
    adapter.send_gcode.return_value = True
    adapter.get_bed_mesh.return_value = {
        "probed_matrix": [[0.1, 0.2], [0.3, 0.1]],
    }
    return adapter


# ---------------------------------------------------------------------------
# LevelingPolicy tests
# ---------------------------------------------------------------------------

class TestLevelingPolicy:
    def test_defaults(self):
        p = LevelingPolicy()
        assert p.enabled is False
        assert p.max_prints_between_levels == 10
        assert p.max_hours_between_levels == 48.0
        assert p.gcode_command == "G29"

    def test_to_dict(self):
        p = LevelingPolicy(enabled=True, max_prints_between_levels=5)
        d = p.to_dict()
        assert d["enabled"] is True
        assert d["max_prints_between_levels"] == 5

    def test_from_dict(self):
        d = {"enabled": True, "max_prints_between_levels": 3, "gcode_command": "BED_MESH_CALIBRATE"}
        p = LevelingPolicy.from_dict(d)
        assert p.enabled is True
        assert p.max_prints_between_levels == 3
        assert p.gcode_command == "BED_MESH_CALIBRATE"

    def test_from_dict_partial(self):
        p = LevelingPolicy.from_dict({"enabled": True})
        assert p.enabled is True
        assert p.max_prints_between_levels == 10  # default

    def test_roundtrip(self):
        p = LevelingPolicy(enabled=True, max_hours_between_levels=24.0)
        p2 = LevelingPolicy.from_dict(p.to_dict())
        assert p.enabled == p2.enabled
        assert p.max_hours_between_levels == p2.max_hours_between_levels


# ---------------------------------------------------------------------------
# LevelingStatus tests
# ---------------------------------------------------------------------------

class TestLevelingStatus:
    def test_to_dict(self):
        s = LevelingStatus(
            printer_name="test",
            needs_leveling=True,
            trigger_reason="threshold",
        )
        d = s.to_dict()
        assert d["printer_name"] == "test"
        assert d["needs_leveling"] is True
        assert d["trigger_reason"] == "threshold"

    def test_to_dict_with_policy(self):
        s = LevelingStatus(
            printer_name="test",
            policy=LevelingPolicy(enabled=True),
        )
        d = s.to_dict()
        assert d["policy"]["enabled"] is True

    def test_defaults(self):
        s = LevelingStatus(printer_name="test")
        assert s.last_leveled_at is None
        assert s.prints_since_level == 0
        assert s.needs_leveling is False
        assert s.mesh_point_count is None


# ---------------------------------------------------------------------------
# BedLevelManager policy tests
# ---------------------------------------------------------------------------

class TestBedLevelManagerPolicy:
    def test_default_policy(self, mgr):
        p = mgr.get_policy("test-printer")
        assert p.enabled is False
        assert p.max_prints_between_levels == 10

    def test_set_and_get_policy(self, mgr):
        policy = LevelingPolicy(enabled=True, max_prints_between_levels=5)
        mgr.set_policy("test-printer", policy)
        loaded = mgr.get_policy("test-printer")
        assert loaded.enabled is True
        assert loaded.max_prints_between_levels == 5

    def test_policy_persists_to_db(self, db):
        mgr1 = BedLevelManager(db=db)
        mgr1.set_policy("p1", LevelingPolicy(enabled=True, max_prints_between_levels=3))

        mgr2 = BedLevelManager(db=db)
        loaded = mgr2.get_policy("p1")
        assert loaded.enabled is True
        assert loaded.max_prints_between_levels == 3

    def test_different_printers_different_policies(self, mgr):
        mgr.set_policy("p1", LevelingPolicy(max_prints_between_levels=5))
        mgr.set_policy("p2", LevelingPolicy(max_prints_between_levels=20))
        assert mgr.get_policy("p1").max_prints_between_levels == 5
        assert mgr.get_policy("p2").max_prints_between_levels == 20


# ---------------------------------------------------------------------------
# BedLevelManager check_needed tests
# ---------------------------------------------------------------------------

class TestBedLevelManagerCheckNeeded:
    def test_disabled_policy_not_needed(self, mgr):
        mgr.set_policy("p1", LevelingPolicy(enabled=False))
        status = mgr.check_needed("p1")
        assert status.needs_leveling is False

    def test_auto_before_first_print(self, mgr):
        mgr.set_policy("p1", LevelingPolicy(
            enabled=True, auto_before_first_print=True,
        ))
        status = mgr.check_needed("p1")
        assert status.needs_leveling is True
        assert "first" in (status.trigger_reason or "").lower()

    def test_prints_threshold_exceeded(self, mgr):
        mgr.set_policy("p1", LevelingPolicy(
            enabled=True, max_prints_between_levels=3,
            auto_before_first_print=False,
        ))
        # Record a leveling so first-print doesn't trigger
        mgr._db.save_leveling({
            "printer_name": "p1", "started_at": time.time(),
            "completed_at": time.time(), "success": True,
            "mesh_data": None, "trigger_reason": "manual",
        })
        # Simulate 3 prints
        mgr._prints_since["p1"] = 3
        status = mgr.check_needed("p1")
        assert status.needs_leveling is True
        assert "prints" in (status.trigger_reason or "").lower()

    def test_below_threshold_not_needed(self, mgr):
        mgr.set_policy("p1", LevelingPolicy(
            enabled=True, max_prints_between_levels=10,
            auto_before_first_print=False,
        ))
        mgr._db.save_leveling({
            "printer_name": "p1", "started_at": time.time(),
            "completed_at": time.time(), "success": True,
            "mesh_data": None, "trigger_reason": "manual",
        })
        mgr._prints_since["p1"] = 2
        status = mgr.check_needed("p1")
        assert status.needs_leveling is False

    def test_time_threshold_exceeded(self, mgr):
        mgr.set_policy("p1", LevelingPolicy(
            enabled=True, max_hours_between_levels=1.0,
            max_prints_between_levels=1000,
            auto_before_first_print=False,
        ))
        # Last leveled 2 hours ago
        mgr._db.save_leveling({
            "printer_name": "p1",
            "started_at": time.time() - 7200,
            "completed_at": time.time() - 7200,
            "success": True, "mesh_data": None,
        })
        status = mgr.check_needed("p1")
        assert status.needs_leveling is True
        assert "hours" in (status.trigger_reason or "").lower()

    def test_mesh_variance_calculated(self, mgr):
        mgr.set_policy("p1", LevelingPolicy(enabled=False))
        mgr._db.save_leveling({
            "printer_name": "p1", "started_at": time.time(),
            "completed_at": time.time(), "success": True,
            "mesh_data": {"probed_matrix": [[0.0, 0.2], [0.4, 0.2]]},
        })
        status = mgr.check_needed("p1")
        assert status.mesh_point_count == 4
        assert status.mesh_variance is not None
        assert status.mesh_variance > 0

    def test_no_db_returns_defaults(self):
        mgr = BedLevelManager(db=None)
        status = mgr.check_needed("p1")
        assert status.needs_leveling is False
        assert status.prints_since_level == 0


# ---------------------------------------------------------------------------
# BedLevelManager event subscription
# ---------------------------------------------------------------------------

class TestBedLevelManagerEvents:
    def test_subscribe_events(self, mgr, bus):
        mgr.subscribe_events()
        bus.subscribe.assert_called_once()

    def test_on_job_completed_increments_counter(self, mgr):
        event = MagicMock()
        event.data = {"printer_name": "p1"}
        mgr._on_job_completed(event)
        assert mgr._prints_since.get("p1") == 1
        mgr._on_job_completed(event)
        assert mgr._prints_since.get("p1") == 2

    def test_on_job_completed_no_printer_name(self, mgr):
        event = MagicMock()
        event.data = {}
        mgr._on_job_completed(event)
        assert len(mgr._prints_since) == 0

    def test_on_job_completed_emits_leveling_needed(self, mgr, bus):
        mgr.set_policy("p1", LevelingPolicy(
            enabled=True, max_prints_between_levels=1,
            auto_before_first_print=False,
        ))
        mgr._db.save_leveling({
            "printer_name": "p1", "started_at": time.time(),
            "completed_at": time.time(), "success": True,
            "mesh_data": None,
        })
        event = MagicMock()
        event.data = {"printer_name": "p1"}
        mgr._on_job_completed(event)
        # Should have published LEVELING_NEEDED
        assert bus.publish.called


# ---------------------------------------------------------------------------
# BedLevelManager trigger tests
# ---------------------------------------------------------------------------

class TestBedLevelManagerTrigger:
    def test_trigger_success(self, mgr, mock_adapter):
        result = mgr.trigger_level("p1", mock_adapter)
        assert result["success"] is True
        mock_adapter.send_gcode.assert_called_once_with(["G29"])

    def test_trigger_custom_command(self, mgr, mock_adapter):
        mgr.set_policy("p1", LevelingPolicy(gcode_command="BED_MESH_CALIBRATE"))
        result = mgr.trigger_level("p1", mock_adapter)
        mock_adapter.send_gcode.assert_called_once_with(["BED_MESH_CALIBRATE"])
        assert result["success"] is True

    def test_trigger_resets_counter(self, mgr, mock_adapter):
        mgr._prints_since["p1"] = 5
        mgr.trigger_level("p1", mock_adapter)
        assert mgr._prints_since["p1"] == 0

    def test_trigger_persists_record(self, mgr, mock_adapter, db):
        mgr.trigger_level("p1", mock_adapter)
        record = db.last_leveling("p1")
        assert record is not None
        assert record["printer_name"] == "p1"

    def test_trigger_gets_mesh_data(self, mgr, mock_adapter):
        result = mgr.trigger_level("p1", mock_adapter)
        mock_adapter.get_bed_mesh.assert_called_once()
        assert result.get("mesh_data") is not None

    def test_trigger_failure(self, mgr):
        adapter = MagicMock()
        adapter.send_gcode.side_effect = Exception("Connection lost")
        adapter.get_bed_mesh.return_value = None
        result = mgr.trigger_level("p1", adapter)
        assert result["success"] is False

    def test_trigger_publishes_events(self, mgr, bus, mock_adapter):
        mgr.trigger_level("p1", mock_adapter)
        # Should have published LEVELING_TRIGGERED and LEVELING_COMPLETED
        assert bus.publish.call_count >= 2

    def test_trigger_failure_publishes_failed(self, mgr, bus):
        adapter = MagicMock()
        adapter.send_gcode.side_effect = Exception("fail")
        adapter.get_bed_mesh.return_value = None
        mgr.trigger_level("p1", adapter)
        # Should have published LEVELING_TRIGGERED and LEVELING_FAILED
        assert bus.publish.call_count >= 2

    def test_without_event_bus(self, db, mock_adapter):
        mgr = BedLevelManager(db=db, event_bus=None)
        result = mgr.trigger_level("p1", mock_adapter)
        assert result["success"] is True

    def test_without_db(self, mock_adapter):
        mgr = BedLevelManager(db=None, event_bus=None)
        result = mgr.trigger_level("p1", mock_adapter)
        assert result["success"] is True
