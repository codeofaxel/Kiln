"""The sign-in funnel: where an account is offered, and how a sign-in goes.

The server sees a device code being made and claimed and nothing before it,
so "nobody was asked", "the agent never passed it on" and "people started
and gave up" all read as the same small number of accounts.  Each stage is
counted on the machine, at the one function every door to it goes through,
and rides the daily heartbeat as ``account_nudge``.

These tests drive the real doors (the connect instructions, ``get_started``,
a refusal that offers a sign-in, the in-chat tools, ``kiln signin`` and
``kiln pair``) and read what was counted.
"""

from __future__ import annotations

import json

import pytest

from kiln import daily_stats


@pytest.fixture
def stats(tmp_path, monkeypatch):
    """Point recording at a temp file (a custom path is never suppressed)."""
    monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "daily_stats.json")
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    # A completed sign-in hands the Terms acceptance to the account; that
    # is a network call this file has no business making.
    monkeypatch.setattr("kiln.terms.sync_to_account", lambda **k: None)

    def counted() -> dict[str, int]:
        return dict(daily_stats.get_daily_stats().get("account_nudge") or {})

    return counted


def _hold_session(tmp_path) -> None:
    path = tmp_path / ".kiln" / "auth_tokens.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"access_token": "a", "refresh_token": "r"}))


class TestTheRecorder:
    def test_a_known_stage_is_counted(self, stats):
        daily_stats.record_account_nudge("started_chat")
        daily_stats.record_account_nudge("started_chat")
        assert stats() == {"started_chat": 2}

    def test_an_unknown_stage_is_dropped_never_a_row(self, stats):
        daily_stats.record_account_nudge("offered_by_carrier_pigeon")
        daily_stats.record_account_nudge("")
        daily_stats.record_account_nudge(None)  # type: ignore[arg-type]
        assert stats() == {}

    def test_the_map_rolls_over_with_the_day(self):
        # The two-day end-to-end lives in test_telemetry_pipeline_fidelity,
        # which walks _ROLLOVER_MAPS; this pins that it is walked.
        assert "account_nudge" in daily_stats._ROLLOVER_MAPS


class TestTheOffers:
    def test_the_connect_instructions_count_an_offer_when_signed_out(self, stats):
        from kiln.server import _build_instructions

        text = _build_instructions()
        assert "ACCOUNT:" in text
        assert stats().get("offered_connect") == 1

    def test_no_offer_is_counted_for_a_signed_in_machine(self, stats, tmp_path):
        _hold_session(tmp_path)
        from kiln.server import _build_instructions

        assert "ACCOUNT:" not in _build_instructions()
        assert "offered_connect" not in stats()

    def test_get_started_counts_its_offer(self, stats):
        import kiln.server as srv

        gs = next(t.fn for t in srv.mcp._tool_manager.list_tools() if t.name == "get_started")
        out = gs()
        assert out["account"]["signed_in"] is False
        assert stats().get("offered_get_started") == 1

    def test_a_refusal_offering_a_sign_in_counts_once_per_reply(self, stats):
        from kiln.tiers_and_terms import signin_hint_fields

        assert signin_hint_fields()["setup_hint"]
        signin_hint_fields()
        assert stats() == {"offered_hint": 2}

    def test_a_lapsed_session_is_asked_back_not_counted_as_a_stranger(
        self, stats, tmp_path
    ):
        _hold_session(tmp_path)
        from kiln.tiers_and_terms import signin_hint_fields

        signin_hint_fields()
        assert stats() == {"offered_resignin": 1}


def _chat_tools():
    from kiln.mcp_compat import FastMCP

    import kiln.plugins.auth_tools as at

    m = FastMCP("t")
    at.register(m)
    return {t.name: t.fn for t in m._tool_manager.list_tools()}


_START = {
    "success": True,
    "verification_uri": "https://kiln3d.com/device?code=KLN-AAAA-BBBB",
    "user_code": "KLN-AAAA-BBBB",
    "device_code": "dc",
    "interval": 1,
    "expires_in": 900,
}
_DONE = {
    "status": "success",
    "access_token": "tok",
    "refresh_token": "ref",
    "email": "a@b.com",
    "tier": "free",
}


class TestTheChatDoor:
    def test_a_started_sign_in_counts_and_a_finished_one_names_its_door(
        self, stats, monkeypatch
    ):
        import kiln.cli.auth_commands as ac

        fns = _chat_tools()
        monkeypatch.setattr(ac, "_http_post", lambda p, b=None, **k: _START)
        assert fns["kiln_signin"]()["success"] is True
        monkeypatch.setattr(ac, "_http_post", lambda p, b=None, **k: {"status": "pending"})
        fns["kiln_signin_poll"](device_code="dc")
        monkeypatch.setattr(ac, "_http_post", lambda p, b=None, **k: _DONE)
        assert fns["kiln_signin_poll"](device_code="dc")["status"] == "success"
        # Pending is not a stage: an agent polls many times per sign-in.
        assert stats() == {"started_chat": 1, "completed_chat": 1}

    def test_a_start_that_fails_is_its_own_stage(self, stats, monkeypatch):
        import kiln.cli.auth_commands as ac

        def boom(*a, **k):
            raise RuntimeError("offline")

        monkeypatch.setattr(ac, "_http_post", boom)
        assert _chat_tools()["kiln_signin"]()["success"] is False
        assert stats() == {"start_failed": 1}

    @pytest.mark.parametrize("ending", ["denied", "expired"])
    def test_how_an_unfinished_sign_in_ended(self, stats, monkeypatch, ending):
        import kiln.cli.auth_commands as ac

        monkeypatch.setattr(ac, "_http_post", lambda p, b=None, **k: {"status": ending})
        _chat_tools()["kiln_signin_poll"](device_code="dc")
        assert stats() == {ending: 1}

    def test_signing_back_in_is_marked_returning(self, stats, monkeypatch, tmp_path):
        import kiln.cli.auth_commands as ac

        _hold_session(tmp_path)
        monkeypatch.setattr(ac, "_http_post", lambda p, b=None, **k: _DONE)
        _chat_tools()["kiln_signin_poll"](device_code="dc")
        assert stats() == {"completed_chat": 1, "completed_returning": 1}


class TestTheTerminalDoors:
    def _run(self, monkeypatch, replies, args=("signin", "--no-browser")):
        from click.testing import CliRunner

        import kiln.cli.auth_commands as ac
        from kiln.cli.main import cli

        queue = list(replies)

        def post(path, body=None, **k):
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(ac, "_http_post", post)
        monkeypatch.setattr(ac.time, "sleep", lambda s: None)
        return CliRunner().invoke(cli, list(args))

    def test_kiln_signin_start_and_finish(self, stats, monkeypatch):
        res = self._run(monkeypatch, [_START, {"status": "pending"}, _DONE])
        assert res.exit_code == 0, res.output
        assert stats() == {"started_cli": 1, "completed_cli": 1}

    def test_kiln_signin_cancelled_in_the_browser(self, stats, monkeypatch):
        res = self._run(monkeypatch, [_START, {"status": "denied"}])
        assert res.exit_code != 0
        assert stats() == {"started_cli": 1, "denied": 1}

    def test_kiln_signin_that_cannot_start(self, stats, monkeypatch):
        import click

        res = self._run(monkeypatch, [click.ClickException("offline")])
        assert res.exit_code != 0
        assert stats() == {"start_failed": 1}

    def test_kiln_signin_refused_by_the_server(self, stats, monkeypatch):
        res = self._run(monkeypatch, [{"success": False, "error": "busy"}])
        assert res.exit_code != 0
        assert stats() == {"start_failed": 1}

    def test_a_new_door_still_counts(self, stats):
        from kiln.cli.auth_commands import _complete_signin

        _complete_signin(dict(_DONE), door="somewhere_new")
        assert stats() == {"completed_other": 1}


class TestItRidesTheHeartbeat:
    def test_the_payload_carries_the_stages(self, stats, monkeypatch):
        import urllib.request

        from kiln import heartbeat

        daily_stats.record_account_nudge("offered_connect")
        daily_stats.record_account_nudge("started_chat")
        sent: list[dict] = []

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            sent.append(json.loads(req.data.decode()))
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(heartbeat, "_already_sent_today", lambda: False)
        monkeypatch.setattr(heartbeat, "_mark_sent", lambda: None)
        monkeypatch.setattr(heartbeat, "_is_ephemeral_runner", lambda: False)
        monkeypatch.setattr(heartbeat, "_is_hosted_multitenant", lambda: False)
        monkeypatch.setattr(heartbeat, "_sent_on", None)
        heartbeat._send_heartbeat()
        beats = [p for p in sent if isinstance(p, dict) and "p_details" in p]
        assert beats, "no heartbeat was posted"
        assert beats[0]["p_details"]["account_nudge"] == {
            "offered_connect": 1,
            "started_chat": 1,
        }
