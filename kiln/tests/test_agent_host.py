"""Which agent host drives the server — the handshake, once, into the heartbeat.

The MCP ``initialize`` handshake is the only thing a stdio server is told
about the app on the other end: a ``clientInfo`` name and version and the
capabilities the host declares.  These tests pin the whole local chain —
the read of that handshake (with the two real hosts measured:
Claude's desktop chat as ``claude-ai`` declaring Apps, Claude
Code as ``claude-code`` declaring elicitation and marking its entry point
in the environment), the once-per-process record, the two maps the day
file carries across midnight, and the heartbeat that ships them — and the
honesty rule underneath: the model is ``unknown`` unless a host volunteers
it, because the protocol never sends one.
"""

from __future__ import annotations

import inspect
import json
from datetime import date as real_date
from types import SimpleNamespace
from unittest import mock

import pytest
from mcp.types import InitializeRequestParams

from kiln import agent_host, daily_stats, heartbeat, local_stage


class _FakeDate(real_date):
    _today = real_date(2026, 9, 24)

    @classmethod
    def today(cls):
        return cls._today


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """Isolated stats file, controllable clock, captured sends, and no
    host recorded before or after each test."""
    monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "stats.json")
    monkeypatch.setattr(daily_stats, "date", _FakeDate)
    monkeypatch.setattr(heartbeat, "date", _FakeDate)
    monkeypatch.setattr(heartbeat, "_is_ci_environment", lambda: False)
    monkeypatch.setattr(heartbeat, "_sent_on", None)
    monkeypatch.setattr(heartbeat, "_LAST_BEAT_PATH", tmp_path / ".last_heartbeat")
    _FakeDate._today = real_date(2026, 9, 24)
    agent_host.reset_recorded()

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
    agent_host.reset_recorded()


def _ctx(params: dict) -> SimpleNamespace:
    """A handler ctx whose session carries a real InitializeRequestParams —
    the object both SDK majors hand the server."""
    return SimpleNamespace(
        session=SimpleNamespace(
            client_params=InitializeRequestParams.model_validate(params)
        )
    )


# Captured from a real Claude desktop-chat handshake: the Apps extension
# declared, nothing else.
CLAUDE_DESKTOP_CHAT = {
    "protocolVersion": "2025-11-25",
    "capabilities": {"extensions": {
        "io.modelcontextprotocol/ui": {
            "mimeTypes": ["text/html;profile=mcp-app"]}}},
    "clientInfo": {"name": "claude-ai", "version": "0.1.0"},
}

# Claude Code's own client record (its binary names itself "claude-code",
# title "Claude Code", and declares elicitation).
CLAUDE_CODE = {
    "protocolVersion": "2025-06-18",
    "capabilities": {"elicitation": {}},
    "clientInfo": {"name": "claude-code", "title": "Claude Code", "version": "2.1.280"},
}

NO_ENV: dict[str, str] = {}
# What the desktop app's Code tab exports to a Kiln server it spawns,
# read from a live process, minus everything not consulted.
CODE_TAB_ENV = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "claude-desktop"}


# ---------------------------------------------------------------------------
# describe(): the handshake, read
# ---------------------------------------------------------------------------


def test_claude_desktop_chat_reads_as_claude_ai_with_apps():
    host = agent_host.describe(SimpleNamespace(), _ctx(CLAUDE_DESKTOP_CHAT), env=NO_ENV)
    assert host is not None
    assert host.name == "claude-ai"
    assert host.version == "0.1.0"
    assert host.apps is True
    assert host.elicitation is False
    assert host.entrypoint == ""
    assert host.label == "claude-ai"
    assert host.model == "unknown"


def test_claude_code_from_the_code_tab_is_told_apart_from_the_terminal():
    tab = agent_host.describe(SimpleNamespace(), _ctx(CLAUDE_CODE), env=CODE_TAB_ENV)
    cli = agent_host.describe(
        SimpleNamespace(), _ctx(CLAUDE_CODE), env={"CLAUDECODE": "1"}
    )
    assert tab is not None and cli is not None
    assert tab.label == "claude-code claude-desktop"
    # Claude Code reads an unset entry point as "cli" — so do we.
    assert cli.label == "claude-code cli"
    assert tab.elicitation is True and tab.apps is False
    assert tab.version == "2.1.280"


def test_a_stale_entrypoint_variable_without_the_marker_is_ignored():
    """Only Claude Code's own marker makes the entry point mean anything;
    another host inheriting a shell variable must not be filed under a
    door it never came through."""
    host = agent_host.describe(
        SimpleNamespace(), _ctx(CLAUDE_CODE),
        env={"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"},
    )
    assert host is not None
    assert host.label == "claude-code"


def test_the_model_is_unknown_unless_the_host_volunteers_it():
    unknown = agent_host.describe(SimpleNamespace(), _ctx(CLAUDE_CODE), env=CODE_TAB_ENV)
    assert unknown is not None and unknown.model == "unknown"

    # Claude Code's documented override names the model it will use.
    override = agent_host.describe(
        SimpleNamespace(), _ctx(CLAUDE_CODE),
        env={**CODE_TAB_ENV, "ANTHROPIC_MODEL": "claude-opus-5-5"},
    )
    assert override is not None and override.model == "claude-opus-5-5"

    # ...but only under Claude Code's marker: a stray variable is not a hint.
    stray = agent_host.describe(
        SimpleNamespace(), _ctx(CLAUDE_DESKTOP_CHAT),
        env={"ANTHROPIC_MODEL": "claude-opus-5-5"},
    )
    assert stray is not None and stray.model == "unknown"

    # A host that puts a model on clientInfo (an open record) is read.
    volunteered = agent_host.describe(
        SimpleNamespace(),
        _ctx({**CLAUDE_CODE, "clientInfo": {**CLAUDE_CODE["clientInfo"], "model": "GPT-5"}}),
        env=NO_ENV,
    )
    assert volunteered is not None and volunteered.model == "gpt-5"


def test_facts_carry_version_model_and_declared_capabilities():
    host = agent_host.describe(SimpleNamespace(), _ctx(CLAUDE_DESKTOP_CHAT), env=NO_ENV)
    assert host is not None
    assert host.facts == [
        "claude-ai v:0.1.0",
        "claude-ai model:unknown",
        "claude-ai apps",
    ]
    code = agent_host.describe(SimpleNamespace(), _ctx(CLAUDE_CODE), env=CODE_TAB_ENV)
    assert code is not None
    assert "claude-code claude-desktop elicitation" in code.facts
    assert "claude-code claude-desktop apps" not in code.facts


def test_no_session_is_no_host_not_unknown():
    """Before the handshake, or on a server driven outside a request, there
    is nothing to describe — and nothing is recorded, because "no session"
    is not a host called unknown."""
    assert agent_host.describe(SimpleNamespace(), None, env=NO_ENV) is None
    assert agent_host.describe(SimpleNamespace(), SimpleNamespace(session=None)) is None


def test_tokens_are_map_keys_not_free_text():
    assert agent_host.token("Claude Desktop!!") == "claude-desktop"
    assert agent_host.token("  ") == "unknown"
    assert agent_host.token(None) == "unknown"
    assert agent_host.token("../etc/passwd") == "etcpasswd"
    assert agent_host.token("%s%s%s") == "sss"
    assert len(agent_host.token("x" * 200)) == 40
    assert agent_host.token("2.1.280") == "2.1.280"


def test_the_longest_composed_key_fits_the_dashboards_budget():
    """Three tokens capped at 40, two spaces and the "model:" prefix: the
    read side and the ingest filter accept 128 characters, and a realistic
    model row is already past the 64 the other label maps use -- so the
    budget is pinned here, where the tokens are cut, not assumed."""
    worst = agent_host.AgentHost(
        name="x" * 40, version="v" * 40, entrypoint="e" * 40,
        apps=True, elicitation=True, model="m" * 40,
    )
    assert len(worst.label) <= agent_host.KEY_BUDGET
    assert max(len(f) for f in worst.facts) == agent_host.KEY_BUDGET
    real = agent_host.AgentHost(
        "claude-code", "2.1.280", "claude-desktop", False, True,
        agent_host.token("us.anthropic.claude-opus-4-1-20250805-v1:0"),
    )
    model_fact = [f for f in real.facts if "model:" in f][0]
    assert 64 < len(model_fact) <= agent_host.KEY_BUDGET


def test_host_declares_apps_is_the_declaration_alone(monkeypatch):
    """The stage-read safety net turns the PANEL on; it says nothing about
    what the host declared, so the description must not inherit it."""
    monkeypatch.setattr(local_stage, "_host_read_the_stage", True)
    assert local_stage.host_declares_apps(SimpleNamespace(), _ctx(CLAUDE_CODE)) is False
    assert local_stage.host_declares_apps(SimpleNamespace(), _ctx(CLAUDE_DESKTOP_CHAT)) is True


# ---------------------------------------------------------------------------
# The record, the day file, the wire
# ---------------------------------------------------------------------------


def test_record_once_counts_the_host_once_per_process(pipeline, monkeypatch):
    monkeypatch.setattr(agent_host.os, "environ", CODE_TAB_ENV)
    ctx = _ctx(CLAUDE_CODE)
    first = agent_host.record_once(SimpleNamespace(), ctx)
    assert first is not None and first.label == "claude-code claude-desktop"
    assert agent_host.record_once(SimpleNamespace(), ctx) is None  # idempotent

    data = daily_stats._read()
    assert data["agent_hosts"] == {"claude-code claude-desktop": 1}
    assert data["agent_host_facts"] == {
        "claude-code claude-desktop v:2.1.280": 1,
        "claude-code claude-desktop model:unknown": 1,
        "claude-code claude-desktop elicitation": 1,
    }


def test_record_once_with_no_session_records_nothing(pipeline):
    assert agent_host.record_once(SimpleNamespace(), None) is None
    data = daily_stats._read()
    assert data["agent_hosts"] == {}
    assert data["agent_host_facts"] == {}
    # ...and stays armed for the first real session.
    assert agent_host._recorded is False


def test_both_maps_are_in_the_day_file_and_carried_across_midnight():
    day = daily_stats._empty_day()
    assert day["agent_hosts"] == {}
    assert day["agent_host_facts"] == {}
    assert "agent_hosts" in daily_stats._ROLLOVER_MAPS
    assert "agent_host_facts" in daily_stats._ROLLOVER_MAPS


def test_the_heartbeat_ships_both_maps_same_day_and_complete(pipeline):
    sent = pipeline
    daily_stats.record_agent_host("claude-ai", ["claude-ai v:0.1.0", "claude-ai apps"])

    heartbeat._send_heartbeat()
    details = sent[0]["p_details"]
    assert details["agent_hosts"] == {"claude-ai": 1}
    assert details["agent_host_facts"] == {"claude-ai v:0.1.0": 1, "claude-ai apps": 1}

    # Midnight passes; the next beat carries the finished day.
    _FakeDate._today = real_date(2026, 9, 25)
    heartbeat._sent_on = None
    (daily_stats._STATS_PATH.parent / ".last_heartbeat").unlink(missing_ok=True)
    heartbeat._send_heartbeat()
    prev = sent[1]["p_details"]["previous_day"]
    assert prev["agent_hosts"] == {"claude-ai": 1}
    assert prev["agent_host_facts"] == {"claude-ai v:0.1.0": 1, "claude-ai apps": 1}
    # A client new enough to ship the maps carries the KEYS even on a day
    # it recorded nothing — that is how the dashboard tells "too old to
    # report" from "quiet day".
    assert sent[1]["p_details"]["agent_hosts"] == {}


def test_the_dispatch_chokepoint_records_the_host():
    """Every tool passes the call_tool wrapper; that is where the host is
    read, so a tool cannot be reached without the host being counted."""
    from kiln import server

    src = inspect.getsource(server._install_mcp_request_context_capture)
    assert "from kiln.agent_host import record_once" in src
    assert "_record_agent_host(mcp, context)" in src
