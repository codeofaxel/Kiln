"""End-to-end: a day of real activity becomes that day's heartbeat row.

Every piece of the telemetry pipeline has unit tests, and for months the
pipeline was still wrong at every layer — because telemetry never raises,
so only a test of the WHOLE chain can go red when a link quietly dies.
This is that test: record activity on day N, cross midnight, and assert
day N's complete counters arrive in the payload the dashboard reads —
then assert day N+1 gets its own send (the startup-only heartbeat lost
every day after the first for long-running servers).
"""

from __future__ import annotations

import json
from datetime import date as real_date
from datetime import timedelta
from unittest import mock

import pytest

from kiln import daily_stats, heartbeat


class _FakeDate(real_date):
    """date.today() under our control, so the test can cross midnight."""

    _today = real_date(2026, 7, 25)

    @classmethod
    def today(cls):
        return cls._today


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """Wire a controllable clock through daily_stats + heartbeat and
    capture what would have been sent to Supabase."""
    monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "stats.json")
    monkeypatch.setattr(daily_stats, "date", _FakeDate)
    monkeypatch.setattr(heartbeat, "date", _FakeDate)
    monkeypatch.setattr(heartbeat, "_is_ci_environment", lambda: False)
    monkeypatch.setattr(heartbeat, "_sent_on", None)
    monkeypatch.setattr(
        heartbeat, "_LAST_BEAT_PATH", tmp_path / ".last_heartbeat",
    )
    _FakeDate._today = real_date(2026, 7, 25)

    sent: list[dict] = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode()))
        return _Resp()

    with mock.patch("urllib.request.urlopen", _fake_urlopen):
        yield sent


def test_a_days_activity_reaches_the_dashboard_complete(pipeline):
    sent = pipeline

    # Day N: a non-Bambu install does real work through the engine
    # chokepoints — no tool remembered to phone anything in.
    daily_stats.record_print_start("moonraker", "benchy.gcode")
    daily_stats.record_print_start("moonraker", "vase.gcode")
    daily_stats.record_event("slices", detail="profile")
    daily_stats.record_tool_event("generate_coaster", {"success": True})
    daily_stats.record_print_hours_for_job("job-1", 3.5)

    # Midnight passes; the server (still running) beats on day N+1.
    _FakeDate._today = real_date(2026, 7, 26)
    heartbeat._send_heartbeat()

    assert len(sent) == 1
    prev = sent[0]["p_details"]["previous_day"]
    assert prev["date"] == "2026-07-25", "filed under the day the work happened"
    assert prev["prints"] == 2
    assert prev["slices"] == 1
    assert prev["generations"] == 1
    assert prev["print_hours"] == 3.5


def test_every_day_of_a_long_running_server_reports(pipeline):
    """The original failure: one heartbeat at startup, six days lost."""
    sent = pipeline

    heartbeat._send_heartbeat()          # day N startup beat
    heartbeat._send_heartbeat()          # same day — daily guard holds
    assert len(sent) == 1

    for offset in (1, 2, 3):             # server never restarts
        _FakeDate._today = real_date(2026, 7, 25) + timedelta(days=offset)
        heartbeat._send_heartbeat()
    assert len(sent) == 4, "each new day must produce its own row"


def test_every_counter_field_the_dashboard_reads_is_in_the_payload(pipeline):
    sent = pipeline
    heartbeat._send_heartbeat()

    payload = sent[0]
    # record_heartbeat's server-side signature is fixed — an unknown p_*
    # argument errors the whole heartbeat — so counters added after that
    # signature froze ride inside p_details instead of as p_*_today fields.
    # Either way they must LEAVE the machine, which is what this pins.
    in_details = {"prints_hours_reported"}
    for counter in daily_stats._VALID_EVENTS - in_details:
        assert f"p_{counter}_today" in payload, (
            f"{counter} is recorded locally but never transmitted"
        )
    for counter in in_details:
        assert counter in payload["p_details"], (
            f"{counter} is recorded locally but never transmitted"
        )
    assert "previous_day" in payload["p_details"]


def test_a_template_build_reaches_the_dashboard(pipeline):
    """Which parametric template got built has to survive the whole chain.

    The 65-part library is free and reachable only through the MCP
    tools, so building one makes no server call — this heartbeat is the
    only evidence it happened.  The `generations` scalar cannot answer
    it: ~25 tools increment that and none of them says which was a
    template, so "nobody uses templates" and "we never measured" read
    the same.
    """
    sent = pipeline

    daily_stats.record_template_use("shelf_bracket")
    daily_stats.record_template_use("shelf_bracket")
    daily_stats.record_template_use("stackable_bin")

    _FakeDate._today = real_date(2026, 7, 26)
    heartbeat._send_heartbeat()

    prev = sent[0]["p_details"]["previous_day"]
    assert prev["template_uses"] == {"shelf_bracket": 2, "stackable_bin": 1}


def test_template_uses_never_carries_a_parameter_value(pipeline):
    """Ids only.  The parameters are the user's own dimensions."""
    sent = pipeline

    daily_stats.record_template_use("shelf_bracket")
    heartbeat._send_heartbeat()

    shipped = sent[0]["p_details"]["template_uses"]
    assert shipped == {"shelf_bracket": 1}
    assert all(isinstance(v, int) for v in shipped.values())


def test_every_breakdown_map_leaves_the_machine(pipeline):
    """The failure that hid five maps at once, pinned generically.

    tool_failures was recorded for months and never returned by
    get_daily_stats, so the heartbeat read {} on every install forever —
    0 of 1,000 production rows carried one.  A per-map assertion would
    not have caught it, because the map nobody thought to assert is
    exactly the one that breaks.  Derive the list instead.
    """
    sent = pipeline
    heartbeat._send_heartbeat()

    details = sent[0]["p_details"]
    for name in daily_stats._ROLLOVER_MAPS:
        assert name in details, (
            f"{name} survives midnight but never leaves the machine"
        )


def test_bridge_running_ships_in_the_payload(pipeline):
    """The field that says whether print hours can recover on their own.

    The duration watchdog dies with the MCP server process; only an
    install running the persistent bridge keeps watching after the chat
    window closes.  Without this field, production cannot say which case
    dominates.  True/False when the pidfile check answered; None means
    "could not determine" — never a guess in either direction.
    """
    sent = pipeline
    heartbeat._send_heartbeat()

    assert "bridge_running" in sent[0]["p_details"]
    assert sent[0]["p_details"]["bridge_running"] in (True, False, None)


def test_tool_failures_actually_leave_the_machine(pipeline):
    """The failure wire was dead end-to-end and every unit test passed.

    ``record_tool_failure`` wrote to disk, ``_ROLLOVER_MAPS`` carried it
    across midnight — and ``get_daily_stats`` never returned it, so the
    heartbeat's ``stats.get("tool_failures")`` read ``{}`` on every
    install, forever.  0 of 1,000 production rows carried one
    (2026-08-24).  Only this whole-chain shape catches that class.
    """
    sent = pipeline

    daily_stats.record_tool_call("start_print")
    daily_stats.record_tool_failure("start_print")

    # Same-day beat: today's maps carry it.
    heartbeat._send_heartbeat()
    details = sent[0]["p_details"]
    assert details["tool_failures"] == {"start_print": 1}
    assert details["tool_calls"] == {"start_print": 1}

    # And across midnight the complete day still carries it.
    _FakeDate._today = real_date(2026, 7, 26)
    heartbeat._send_heartbeat()
    prev = sent[1]["p_details"]["previous_day"]
    assert prev["tool_failures"] == {"start_print": 1}
    assert prev["tool_calls"] == {"start_print": 1}


def test_every_map_the_heartbeat_reads_exists_in_daily_stats(pipeline):
    """Lockstep for the MAPS, the way _VALID_EVENTS pins the scalars.

    The heartbeat builds p_details with stats.get(<map>, {}) — a key
    get_daily_stats doesn't return reads as {} forever and no test
    of either side alone goes red.  tool_failures died exactly this
    way.  Every rollover map must be present in the stats surface.
    """
    stats = daily_stats.get_daily_stats()
    for key in daily_stats._ROLLOVER_MAPS:
        assert key in stats, (
            f"{key} rolls over locally but get_daily_stats never returns "
            "it — the heartbeat will transmit {} forever"
        )
