"""Tests for kiln.uptime."""

from __future__ import annotations

from kiln.uptime import UptimeTracker


class TestUptimeTracker:
    def test_empty_report(self, tmp_path):
        tracker = UptimeTracker(data_file=tmp_path / "uptime.json")
        report = tracker.uptime_report()
        assert report["total_checks"] == 0
        assert report["uptime_24h"] is None
        assert report["last_check"] is None

    def test_record_healthy(self, tmp_path):
        tracker = UptimeTracker(data_file=tmp_path / "uptime.json")
        check = tracker.record_check(healthy=True, response_ms=50.0)
        assert check.healthy is True
        assert check.response_ms == 50.0
        assert tracker.uptime_report()["total_checks"] == 1

    def test_uptime_100_percent(self, tmp_path):
        tracker = UptimeTracker(data_file=tmp_path / "uptime.json")
        for _ in range(10):
            tracker.record_check(healthy=True)
        report = tracker.uptime_report()
        assert report["uptime_1h"] == 100.0
        assert report["sla_met"] is True

    def test_uptime_with_failures(self, tmp_path):
        tracker = UptimeTracker(data_file=tmp_path / "uptime.json")
        for _ in range(9):
            tracker.record_check(healthy=True)
        tracker.record_check(healthy=False, details="DB timeout")
        report = tracker.uptime_report()
        assert report["uptime_1h"] == 90.0
        assert report["sla_met"] is False

    def test_recent_incidents(self, tmp_path):
        tracker = UptimeTracker(data_file=tmp_path / "uptime.json")
        tracker.record_check(healthy=True)
        tracker.record_check(healthy=False, details="timeout")
        tracker.record_check(healthy=True)
        incidents = tracker.recent_incidents()
        assert len(incidents) == 1
        assert incidents[0]["details"] == "timeout"

    def test_persistence(self, tmp_path):
        data_file = tmp_path / "uptime.json"
        t1 = UptimeTracker(data_file=data_file)
        t1.record_check(healthy=True)
        t1.record_check(healthy=False)

        t2 = UptimeTracker(data_file=data_file)
        assert t2.uptime_report()["total_checks"] == 2


class TestSLATracking:
    def test_sla_met_at_threshold(self, tmp_path):
        tracker = UptimeTracker(data_file=tmp_path / "uptime.json")
        for _ in range(999):
            tracker.record_check(healthy=True)
        tracker.record_check(healthy=False)
        report = tracker.uptime_report()
        assert report["uptime_1h"] == 99.9
        assert report["sla_met"] is True

    def test_sla_not_met_below_threshold(self, tmp_path):
        tracker = UptimeTracker(data_file=tmp_path / "uptime.json")
        for _ in range(998):
            tracker.record_check(healthy=True)
        tracker.record_check(healthy=False)
        tracker.record_check(healthy=False)
        report = tracker.uptime_report()
        assert report["uptime_1h"] < 99.9
        assert report["sla_met"] is False
