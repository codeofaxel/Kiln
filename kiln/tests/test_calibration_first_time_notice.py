"""Tests for the first-time calibration overlay notice.

Verifies:
  - First slice that triggers an overlay attaches a `first_time_notice`
  - Marker file is created at ~/.kiln/calibration_overlay_first_use.seen
  - Subsequent slices do NOT attach the notice (silent overlay)
  - Marker write failure doesn't block the slice
  - Notice carries agent-paraphrasable structured fields
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiln.plugins.slicer_tools import _attach_first_time_notice_if_unseen


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Redirect $HOME so the marker file lands in tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _sample_cal_used():
    """Realistic-ish cal_used block matching what calibration_used_block returns."""
    return {
        "tier": "high",
        "source": "orcaslicer",
        "profile_name": "Bambu PLA Basic (tuned)",
        "material": "PLA",
        "applied": {
            "extrusion_multiplier": 0.98,
            "pressure_advance": 0.022,
            "xy_size_compensation": -0.05,
        },
    }


def test_first_use_attaches_notice(fresh_home):
    cal_used = _sample_cal_used()
    out = _attach_first_time_notice_if_unseen(cal_used)
    assert "first_time_notice" in out, "first slice should surface the notice"

    notice = out["first_time_notice"]
    assert "headline" in notice
    assert "what_happened" in notice
    assert "ongoing_behavior" in notice
    assert "opt_out" in notice
    assert "remind_me" in notice

    # Marker should now exist
    marker = fresh_home / ".kiln" / "calibration_overlay_first_use.seen"
    assert marker.exists(), "marker file must be created on first use"


def test_second_use_silent(fresh_home):
    """Second call after marker exists should NOT attach the notice."""
    # Pre-create the marker
    marker_dir = fresh_home / ".kiln"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "calibration_overlay_first_use.seen").touch()

    cal_used = _sample_cal_used()
    out = _attach_first_time_notice_if_unseen(cal_used)
    assert "first_time_notice" not in out, "subsequent slices should be silent"


def test_first_use_notice_includes_profile_name(fresh_home):
    cal_used = _sample_cal_used()
    out = _attach_first_time_notice_if_unseen(cal_used)
    headline = out["first_time_notice"]["headline"]
    assert "Bambu PLA Basic" in headline
    assert "orcaslicer" in headline


def test_first_use_notice_includes_applied_values(fresh_home):
    cal_used = _sample_cal_used()
    out = _attach_first_time_notice_if_unseen(cal_used)
    what_happened = out["first_time_notice"]["what_happened"]
    # Either the keys or values from `applied` should appear
    assert "extrusion_multiplier" in what_happened or "0.98" in what_happened


def test_handles_minimal_cal_used_block(fresh_home):
    """When cal_used has only the bare minimum, notice still composes."""
    minimal = {"tier": "medium"}
    out = _attach_first_time_notice_if_unseen(minimal)
    assert "first_time_notice" in out
    notice = out["first_time_notice"]
    assert "Kiln Pro" in notice["headline"]
    # Falls back to generic phrasing
    assert "your slicer" in notice["headline"].lower() or \
           "your slicer" in notice["what_happened"].lower()


def test_marker_write_failure_doesnt_block(monkeypatch, tmp_path):
    """If we can't write the marker, the notice still gets attached
    (it'll just surface again next time — that's fine)."""
    # Point HOME at a readonly location so marker write fails silently
    monkeypatch.setenv("HOME", "/proc/cant-write-here-this-doesnt-exist-xyz")

    cal_used = _sample_cal_used()
    # Should not raise even though marker write will fail
    out = _attach_first_time_notice_if_unseen(cal_used)
    assert "first_time_notice" in out


def test_idempotent_when_called_twice(fresh_home):
    cal_used = _sample_cal_used()
    first = _attach_first_time_notice_if_unseen(cal_used)
    second = _attach_first_time_notice_if_unseen(cal_used)
    assert "first_time_notice" in first
    assert "first_time_notice" not in second


def test_does_not_mutate_input(fresh_home):
    cal_used = _sample_cal_used()
    original_keys = set(cal_used.keys())
    _attach_first_time_notice_if_unseen(cal_used)
    # The input dict shouldn't have grown a first_time_notice key —
    # the notice goes on a NEW dict, leaving the input untouched.
    assert set(cal_used.keys()) == original_keys
