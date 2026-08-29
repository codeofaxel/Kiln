"""The surface split: which door (CLI / MCP / web) activity came through.

The CLI and the MCP server share every engine chokepoint, so before the
surface dimension existed no recorded event could say which surface it
served — which is how the CLI's hand-forked profile mapping stayed
broken for ~6 months with nobody able to tell whether the CLI had users
at all.  These tests pin the whole local chain: one process-level
resolver, per-surface counters, and a heartbeat payload that keeps the
"unknown" absence distinguishable from the real surfaces.
"""

from __future__ import annotations

import json
from datetime import date as real_date
from unittest import mock

import pytest

from kiln import daily_stats, heartbeat, surface


class _FakeDate(real_date):
    _today = real_date(2026, 8, 27)

    @classmethod
    def today(cls):
        return cls._today


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """Isolated stats file, controllable clock, captured sends, and a
    clean surface declaration before AND after each test."""
    monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "stats.json")
    monkeypatch.setattr(daily_stats, "date", _FakeDate)
    monkeypatch.setattr(daily_stats, "_surface_session_recorded", False)
    monkeypatch.setattr(heartbeat, "date", _FakeDate)
    monkeypatch.setattr(heartbeat, "_is_ci_environment", lambda: False)
    monkeypatch.setattr(heartbeat, "_sent_on", None)
    monkeypatch.setattr(heartbeat, "_LAST_BEAT_PATH", tmp_path / ".last_heartbeat")
    _FakeDate._today = real_date(2026, 8, 27)
    monkeypatch.delenv("KILN_SURFACE", raising=False)
    surface.reset_surface()

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
    surface.reset_surface()


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


def test_undeclared_process_reads_unknown(pipeline):
    assert surface.get_surface() == "unknown"


def test_kiln_serve_ends_up_mcp_not_cli(pipeline):
    """`kiln serve` enters through the CLI door, then the server's own
    entry point re-declares.  The LAST declaration must win, or every
    MCP session launched by `kiln serve` would count as CLI usage —
    the exact conflation the split exists to remove."""
    surface.set_surface("cli")   # kiln.cli.main:main
    surface.set_surface("mcp")   # kiln.server:main, moments later
    assert surface.get_surface() == "mcp"


def test_garbage_declaration_is_dropped_not_raised(pipeline):
    surface.set_surface("cli")
    surface.set_surface("/tmp/pp-fuzz")   # not a surface token
    surface.set_surface("Web Browser!")   # shape matters
    surface.set_surface("unknown")        # the absence, not a door
    assert surface.get_surface() == "cli"


def test_an_embedding_launcher_can_declare_its_own_door(pipeline):
    """Acceptance is by shape, not a closed set: a launcher that embeds
    Kiln declares a door this file has never heard of, and the token is
    carried as-is rather than collapsed into "unknown".  The dashboard
    whitelists what it renders, so junk can't mint a row there."""
    surface.set_surface("kiosk")
    assert surface.get_surface() == "kiosk"


def test_env_override_outranks_the_entry_point(pipeline, monkeypatch):
    """A launcher that spawns `kiln serve` as a child knows the child's
    real door better than the child's own entry point does."""
    surface.set_surface("mcp")
    monkeypatch.setenv("KILN_SURFACE", "kiosk")
    assert surface.get_surface() == "kiosk"
    monkeypatch.setenv("KILN_SURFACE", "Not A Token")
    assert surface.get_surface() == "mcp"


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def test_events_are_attributed_to_the_declared_surface(pipeline):
    surface.set_surface("cli")
    daily_stats.record_event("slices")
    surface.set_surface("mcp")
    daily_stats.record_event("slices")
    daily_stats.record_event("prints")

    stats = daily_stats.get_daily_stats()
    assert stats["surface_events"] == {
        "cli": {"slices": 1},
        "mcp": {"slices": 1, "prints": 1},
    }
    # The per-surface split must SUM to the scalar counters, or the
    # dashboard shows two different totals for the same day.
    assert stats["slices"] == 2
    assert stats["prints"] == 1


def test_texture_double_count_is_mirrored_per_surface(pipeline):
    """textures also increments decorations at the scalar level; the
    per-surface map mirrors that so the sum invariant holds per key."""
    surface.set_surface("mcp")
    daily_stats.record_event("textures", detail="tiger_stripe")

    stats = daily_stats.get_daily_stats()
    assert stats["surface_events"]["mcp"] == {"textures": 1, "decorations": 1}
    assert stats["textures"] == 1
    assert stats["decorations"] == 1


def test_undeclared_recorder_lands_in_unknown_bucket(pipeline):
    """The bridge supervisor (and any library import) declares nothing.
    Its events must land in "unknown" — never inflate a real surface."""
    daily_stats.record_event("prints")
    stats = daily_stats.get_daily_stats()
    assert stats["surface_events"] == {"unknown": {"prints": 1}}


def test_surface_session_counts_once_per_process(pipeline):
    surface.set_surface("cli")
    daily_stats.record_surface_session()
    daily_stats.record_surface_session()  # same process — idempotent
    stats = daily_stats.get_daily_stats()
    assert stats["surface_sessions"] == {"cli": 1}


# ---------------------------------------------------------------------------
# The whole chain: recorded → survives midnight → leaves the machine
# ---------------------------------------------------------------------------


def test_surface_split_reaches_the_dashboard_complete(pipeline):
    sent = pipeline

    surface.set_surface("cli")
    daily_stats.record_surface_session()
    daily_stats.record_event("slices")

    # Midnight passes; the beat carries the finished day.
    _FakeDate._today = real_date(2026, 8, 28)
    heartbeat._send_heartbeat()

    assert len(sent) == 1
    details = sent[0]["p_details"]
    assert "surface_sessions" in details
    assert "surface_events" in details
    prev = details["previous_day"]
    assert prev["surface_sessions"] == {"cli": 1}
    assert prev["surface_events"] == {"cli": {"slices": 1}}


def test_same_day_beat_carries_the_split_too(pipeline):
    sent = pipeline
    surface.set_surface("mcp")
    daily_stats.record_surface_session()
    daily_stats.record_event("prints")

    heartbeat._send_heartbeat()
    details = sent[0]["p_details"]
    assert details["surface_sessions"] == {"mcp": 1}
    assert details["surface_events"] == {"mcp": {"prints": 1}}


# ---------------------------------------------------------------------------
# Entry-point wiring — the doors themselves declare, nothing else does
# ---------------------------------------------------------------------------


def test_cli_entry_point_declares_cli(pipeline, monkeypatch):
    """kiln.cli.main:main is the door for `kiln`, `kiln3d`, and
    `python -m kiln`; the declaration lives there, not per command."""
    from kiln.cli import main as cli_main

    monkeypatch.setattr(cli_main, "_ensure_utf8_streams", lambda: None)
    monkeypatch.setattr(cli_main, "cli", lambda: None)
    cli_main.main()
    assert surface.get_surface() == "cli"


def test_vocabulary_matches_the_kiln_pro_side(pipeline):
    """The shared words must mean the same doors on both sides.  kiln-pro's
    presence vocabulary owns "web"; this repo never sets it but must not
    invent a different spelling for the same surface."""
    assert surface.KNOWN_SURFACES == frozenset({"cli", "mcp", "web"})
    assert surface.UNKNOWN == "unknown"
