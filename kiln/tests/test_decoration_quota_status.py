"""decoration_quota_status()'s contract — what decorate_surface now attaches
to a successful call's response so a free/local caller sees where they stand
("2 of 3 used") before the wall instead of only hitting it as an error next
time. No existing test covered this module before; this locks down the exact
shape decorate_surface's new "quota" field depends on.
"""
from __future__ import annotations

from pathlib import Path

from kiln.decoration_quota import DecorationQuota


def test_status_reports_used_limit_remaining_for_free_tier(tmp_path: Path) -> None:
    tracker = DecorationQuota(quota_path=tmp_path / "decoration_usage.json")
    tracker._get_tier = lambda: "free"  # noqa: SLF001 — test seam, avoids a real license lookup
    ok, _ = tracker.check_and_increment()
    assert ok is True
    status = tracker.get_status().to_dict()
    assert status == {
        "used": 1,
        "limit": 3,
        "remaining": 2,
        "tier": "free",
        "month": status["month"],
        "unlimited": False,
    }


def test_status_shows_unlimited_not_a_number_for_pro(tmp_path: Path) -> None:
    tracker = DecorationQuota(quota_path=tmp_path / "decoration_usage.json")
    tracker._get_tier = lambda: "pro"  # noqa: SLF001
    status = tracker.get_status().to_dict()
    # Must never show a scary countdown to a paying user — "unlimited", not "0".
    assert status["limit"] == "unlimited"
    assert status["remaining"] == "unlimited"
    assert status["unlimited"] is True


def test_status_at_the_cap_shows_zero_remaining(tmp_path: Path) -> None:
    tracker = DecorationQuota(quota_path=tmp_path / "decoration_usage.json")
    tracker._get_tier = lambda: "free"  # noqa: SLF001
    for _ in range(3):
        ok, _ = tracker.check_and_increment()
        assert ok is True
    status = tracker.get_status().to_dict()
    assert status["used"] == 3 and status["remaining"] == 0
