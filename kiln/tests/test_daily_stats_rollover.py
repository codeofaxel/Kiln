"""Day-rollover archival for the daily counters.

Regression guard for a systematic undercount: the heartbeat fires once,
at Kiln server startup, and reads whatever counters exist at that
instant.  Since you must start the server to use Kiln, the same-day
counters are ~always zero at that moment — production carried a print
count on 17 of 671 heartbeats, which reads as "our users don't print"
when it actually means "we sample before the work happens".  A finished
day is now archived so the next heartbeat can report a complete one.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from kiln import daily_stats


def _write_stats(tmp_path, monkeypatch, payload: dict):
    path = tmp_path / "daily_stats.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(daily_stats, "_STATS_PATH", path)
    return path


def test_same_day_stats_are_returned_untouched(tmp_path, monkeypatch):
    today = str(date.today())
    _write_stats(tmp_path, monkeypatch, {
        "date": today, "prints": 3, "generations": 1,
    })
    stats = daily_stats.get_daily_stats()
    assert stats["prints"] == 3
    assert stats["generations"] == 1
    # Nothing archived — the day isn't over.
    assert stats["previous_day"] == {}


def test_rolled_over_day_is_archived_not_discarded(tmp_path, monkeypatch):
    yesterday = str(date.today() - timedelta(days=1))
    _write_stats(tmp_path, monkeypatch, {
        "date": yesterday,
        "prints": 4, "generations": 2, "slices": 7,
        "decorations": 1, "textures": 1, "downloads": 3,
        "print_hours": 12.5,
    })
    stats = daily_stats.get_daily_stats()

    # Today starts clean...
    assert stats["prints"] == 0
    assert stats["generations"] == 0
    # ...and yesterday's COMPLETE totals survive for the heartbeat.
    prev = stats["previous_day"]
    assert prev["date"] == yesterday
    assert prev["prints"] == 4
    assert prev["generations"] == 2
    assert prev["slices"] == 7
    assert prev["print_hours"] == 12.5


def test_recording_after_rollover_keeps_the_archive(tmp_path, monkeypatch):
    """The archive must survive a write — otherwise the first event of
    the new day erases the day we were about to report."""
    yesterday = str(date.today() - timedelta(days=1))
    _write_stats(tmp_path, monkeypatch, {"date": yesterday, "prints": 5})

    daily_stats.record_event("prints")
    stats = daily_stats.get_daily_stats()

    assert stats["prints"] == 1, "today's new event counted"
    assert stats["previous_day"]["prints"] == 5, "yesterday still reportable"
    assert stats["previous_day"]["date"] == yesterday


def test_missing_or_corrupt_stats_file_is_calm(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "nope.json")
    assert daily_stats.get_daily_stats()["previous_day"] == {}

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(daily_stats, "_STATS_PATH", bad)
    assert daily_stats.get_daily_stats()["previous_day"] == {}
