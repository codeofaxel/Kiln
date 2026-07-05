"""Per-tool anonymous call counter — record_tool_call + its heartbeat wiring.

Proves the new anonymous "what tools do not-signed-in local users actually
use" signal: names + counts only, junk rejected, distinct-name cap honored,
and the busiest tools reach the heartbeat payload (top-N).
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from kiln import daily_stats, heartbeat


@pytest.fixture(autouse=True)
def _isolated_stats(tmp_path, monkeypatch):
    """Point the stats file at a temp path so tests never touch ~/.kiln."""
    monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "daily_stats.json")


class TestRecordToolCall:
    def test_counts_real_tool_names(self):
        daily_stats.record_tool_call("generate_coaster")
        daily_stats.record_tool_call("generate_coaster")
        daily_stats.record_tool_call("slice_model")
        stats = daily_stats.get_daily_stats()
        assert stats["tool_calls"] == {"generate_coaster": 2, "slice_model": 1}

    def test_empty_day_has_tool_calls_bucket(self):
        assert daily_stats.get_daily_stats()["tool_calls"] == {}

    @pytest.mark.parametrize(
        "junk",
        ["", "  ", "%s%s%s", "/tmp/pp-fuzz", "Generate_Coaster", "ab", "1tool", "has space"],
    )
    def test_rejects_non_tool_names(self, junk):
        daily_stats.record_tool_call(junk)
        assert daily_stats.get_daily_stats()["tool_calls"] == {}

    def test_distinct_name_cap_drops_new_but_keeps_counting_existing(self, monkeypatch):
        monkeypatch.setattr(daily_stats, "_TOOL_CALLS_MAX_DISTINCT", 3)
        for name in ("tool_aaa", "tool_bbb", "tool_ccc"):
            daily_stats.record_tool_call(name)
        # Cap reached: a brand-new 4th name is dropped...
        daily_stats.record_tool_call("tool_ddd")
        # ...but an already-tracked name keeps incrementing.
        daily_stats.record_tool_call("tool_aaa")
        calls = daily_stats.get_daily_stats()["tool_calls"]
        assert "tool_ddd" not in calls
        assert calls["tool_aaa"] == 2
        assert len(calls) == 3


class TestTopN:
    def test_returns_busiest_n(self):
        m = {"a": 1, "b": 9, "c": 5, "d": 3}
        assert heartbeat._top_n(m, 2) == {"b": 9, "c": 5}

    def test_bad_input_is_empty(self):
        assert heartbeat._top_n(None, 5) == {}
        assert heartbeat._top_n({}, 5) == {}
        assert heartbeat._top_n("nope", 5) == {}


class TestHeartbeatShipsToolCalls:
    def test_payload_details_carries_top_tool_calls(self, monkeypatch):
        # Seed a stats file with more distinct tools than the top-N cap
        # would ship, so we prove both inclusion AND the cap.
        tool_calls = {f"tool_{i:03d}": i for i in range(1, 130)}  # 129 tools
        (daily_stats._STATS_PATH).write_text(
            json.dumps({"date": str(daily_stats.date.today()), "tool_calls": tool_calls}),
            encoding="utf-8",
        )
        # _send_heartbeat short-circuits under pytest (CI guard) — turn it off.
        monkeypatch.setattr(heartbeat, "_is_ci_environment", lambda: False)
        monkeypatch.setattr(heartbeat, "_already_sent_today", lambda: False)

        captured = {}

        class _Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def _fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _Resp()

        with mock.patch("urllib.request.urlopen", _fake_urlopen):
            heartbeat._send_heartbeat()

        details = json.loads(captured["body"]["p_details"])
        shipped = details["tool_calls"]
        assert len(shipped) == 100          # capped to top-100
        assert "tool_129" in shipped         # busiest survived
        assert "tool_001" not in shipped     # least-busy trimmed
