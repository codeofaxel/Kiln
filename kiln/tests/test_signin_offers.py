"""A signed-out install is offered a free account once per kind of value.

Kiln meets moments where a free account would have done more for the thing on
the table: a printer held to generic limits, a make that ran on this computer
alone, a design-history tool that needs the account.  ``kiln.signin_offer``
paces, words and counts every one of those offers.  These tests drive the real
doors (``safety_profiles.warm_profile``, ``served_public.say_once``, the
manifest stub's sign-in wall, the history courier's cloud request) and read
what the person and the agent were told, and what was counted.
"""

from __future__ import annotations

import json
import time

import pytest

from kiln import daily_stats, signin_offer
from kiln.served_answer import Miss

_NEVER = Miss("signed_out", "KILN_ACCOUNT_NOT_PAIRED")
_EXPIRED = Miss("signed_out", "KILN_SESSION_EXPIRED")
_SERVED_ID = "kilntest_served"  # a roster-only row in the session fixture


@pytest.fixture
def stats(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "daily_stats.json")
    monkeypatch.setattr(daily_stats, "_LIMITS_NUDGE_MARKER_PATH", tmp_path / "offer_marker.json")
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    monkeypatch.setattr("kiln.terms.sync_to_account", lambda **k: None)

    def counted() -> dict[str, int]:
        return dict(daily_stats.get_daily_stats().get("account_nudge") or {})

    return counted


def _hold_session(tmp_path, **tokens) -> None:
    path = tmp_path / ".kiln" / "auth_tokens.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens or {"access_token": "a", "refresh_token": "r"}))


def _later(monkeypatch, seconds: float) -> None:
    now = time.time() + seconds
    monkeypatch.setattr(signin_offer, "_now", lambda: now)


# ---------------------------------------------------------------------------
# The pacing
# ---------------------------------------------------------------------------


class TestThePacing:
    def test_once_then_once_more_after_two_weeks_then_never(self, stats, monkeypatch):
        assert signin_offer.claim("design")
        assert not signin_offer.claim("design")
        _later(monkeypatch, signin_offer.REOFFER_AFTER_S - 60)
        assert not signin_offer.claim("design"), "not yet two weeks"
        _later(monkeypatch, signin_offer.REOFFER_AFTER_S + 60)
        assert signin_offer.claim("design")
        _later(monkeypatch, 10 * signin_offer.REOFFER_AFTER_S)
        assert not signin_offer.claim("design"), "two offers per kind, ever"

    def test_each_kind_and_each_printer_model_is_its_own_reason(self, stats):
        assert signin_offer.claim("design")
        assert signin_offer.claim("history")
        assert signin_offer.claim("printer", "Bambu Lab A1")
        assert signin_offer.claim("printer", "Prusa MK4")
        assert not signin_offer.claim("printer", "bambu lab a1"), "same model, any spelling case"

    def test_the_ledger_survives_a_restart(self, stats):
        assert signin_offer.claim("design")
        # A new process reads the same file: nothing in memory carries it.
        assert json.loads(signin_offer._ledger_path().read_text())["design"]["times"]
        assert not signin_offer.claim("design")

    def test_an_unknown_kind_is_never_offered(self, stats):
        assert not signin_offer.claim("upgrade")
        assert signin_offer.offer("upgrade", "anything") is None


class TestWhoIsOffered:
    def test_a_signed_in_install_is_never_offered(self, stats, tmp_path):
        _hold_session(tmp_path)
        for kind in signin_offer.KINDS:
            assert signin_offer.offer(kind, "x") is None
        assert stats() == {}

    def test_a_lapsed_session_is_not_a_stranger(self, stats, tmp_path):
        _hold_session(tmp_path, refresh_token="r")
        assert signin_offer.offer("design", "x") is None

    def test_a_licence_key_is_not_signed_out(self, stats, monkeypatch):
        monkeypatch.setenv("KILN_LICENSE_KEY", "kiln_live_test")
        assert signin_offer.offer("design", "x") is None

    def test_kilns_own_server_never_offers(self, stats, monkeypatch):
        monkeypatch.setattr("kiln.heartbeat._is_hosted_multitenant", lambda: True)
        assert signin_offer.offer("design", "x") is None
        assert not signin_offer._ledger_path().exists()

    def test_the_offer_names_the_tool_an_agent_can_call_and_no_price(self, stats):
        block = signin_offer.offer("design", "Made here.")
        assert block["tool"] == "kiln_signin"
        assert "kiln_signin" in block["agent_hint"]
        for word in ("Pro", "pricing", "$", "upgrade", "plan"):
            assert word not in block["agent_hint"] + block["message"]
        assert stats() == {"offered_design": 1}


# ---------------------------------------------------------------------------
# The printer: the setup-time limits nudge
# ---------------------------------------------------------------------------


@pytest.fixture
def printer(stats, monkeypatch, tmp_path):
    from kiln import _pro_catalogue_bridge as bridge
    from kiln import printer_intelligence as pi
    from kiln import safety_profiles as sp

    monkeypatch.setattr(bridge, "available", lambda: False)
    monkeypatch.setattr(bridge, "KEEP_DIR", tmp_path / "printer_catalogue")
    monkeypatch.setattr(bridge, "_served", lambda key: (None, _NEVER))

    def restart() -> None:
        """What a new server process starts with: nothing in memory."""
        sp._cache.clear()
        sp._roster.clear()
        sp._variant_data.clear()
        sp._loaded = False
        sp._nudged.clear()
        sp._pending_nudges.clear()
        pi._reset_caches()
        bridge.forget()

    restart()
    yield restart
    restart()


class TestThePrinterOffer:
    def test_said_once_per_install_not_once_per_server_start(self, stats, printer):
        from kiln import safety_profiles as sp

        first = sp.warm_profile(_SERVED_ID)
        assert first and "Sign in free" in first
        printer()  # the next session's server
        assert sp.warm_profile(_SERVED_ID) is None
        assert stats() == {"offered_limits": 1}

    def test_said_again_two_weeks_later_at_a_fresh_setup(self, stats, printer, monkeypatch):
        from kiln import safety_profiles as sp

        assert sp.warm_profile(_SERVED_ID)
        printer()
        _later(monkeypatch, signin_offer.REOFFER_AFTER_S + 60)
        assert sp.warm_profile(_SERVED_ID)
        assert stats() == {"offered_limits": 2}

    def test_the_generic_floor_holds_whether_or_not_it_is_said(self, stats, printer):
        """The offer is about wording only: the limits in force are the same."""
        from kiln import safety_profiles as sp

        sp.warm_profile(_SERVED_ID)
        said = sp.resolve_limits(_SERVED_ID)
        printer()
        assert sp.warm_profile(_SERVED_ID) is None
        assert sp.resolve_limits(_SERVED_ID) == said == (
            sp._UNKNOWN_PRINTER_MAX_HOTEND_C, sp._UNKNOWN_PRINTER_MAX_BED_C,
        )

    def test_a_lapsed_session_is_told_to_sign_back_in(self, stats, printer, tmp_path):
        from kiln import safety_profiles as sp

        _hold_session(tmp_path, refresh_token="r")
        sentence = sp.warm_profile(_SERVED_ID)
        assert sentence and "Sign back in" in sentence and "expired" in sentence
        assert "free" not in sentence.lower()
        assert stats() == {"offered_resignin": 1}


# ---------------------------------------------------------------------------
# A make that ran on this computer
# ---------------------------------------------------------------------------


@pytest.fixture
def said(monkeypatch):
    from kiln import served_public

    monkeypatch.setattr(served_public, "_said", set())
    return served_public


class TestTheDesignOffer:
    def test_a_make_carries_the_offer_instead_of_the_miss_note(self, stats, said):
        made = {"success": True, "stl_path": "/tmp/part.stl"}
        out = said.say_once("compile_scad", dict(made), _NEVER)
        assert out["signin_offer"]["kind"] == "design"
        assert said.NOTE_KEY not in out
        # Nothing the make returned is taken away.
        assert {k: v for k, v in out.items() if k != "signin_offer"} == made
        assert stats() == {"offered_design": 1}

    def test_after_the_offer_a_make_says_nothing_about_signing_in(self, stats, said, monkeypatch):
        said.say_once("compile_scad", {"success": True}, _NEVER)
        monkeypatch.setattr(said, "_said", set())  # the next session
        out = said.say_once("add_mesh_fillet", {"success": True}, _NEVER)
        assert out == {"success": True}

    def test_a_verdict_keeps_its_note_about_where_it_was_answered(self, stats, said):
        out = said.say_once("analyze_printability", {"success": True}, _NEVER)
        assert said.NOTE_KEY in out and "signin_offer" not in out

    def test_a_lapsed_session_hears_sign_back_in_not_an_offer(self, stats, said):
        out = said.say_once("compile_scad", {"success": True}, _EXPIRED)
        assert "signin_offer" not in out
        assert "sign back in" in out[said.NOTE_KEY]["note"].lower()

    def test_a_failed_make_is_not_a_moment_of_value(self, stats, said):
        out = said.say_once("compile_scad", {"success": False, "error": "syntax"}, _NEVER)
        assert "signin_offer" not in out and stats() == {}

    def test_a_sign_in_within_a_day_converts_the_design_offer(self, stats, said):
        from kiln.cli.auth_commands import _complete_signin

        said.say_once("compile_scad", {"success": True}, _NEVER)
        _complete_signin({"access_token": "a", "refresh_token": "r", "email": "x@y.z"}, door="chat")
        assert stats() == {"offered_design": 1, "completed_chat": 1, "completed_design": 1}


# ---------------------------------------------------------------------------
# Design history: the sign-in refusal
# ---------------------------------------------------------------------------


@pytest.fixture
def history_tool(stats, monkeypatch):
    import kiln.server as srv

    monkeypatch.setattr(srv, "_PRO_TOOL_HISTORY", {"show_branch_history"})
    monkeypatch.setattr(srv, "_PRO_TOOL_TIERS", {"show_branch_history": "free"})
    monkeypatch.setattr(srv, "_PRO_TOOL_NUDGES", {})
    monkeypatch.setattr(srv, "_PRO_TOOL_QUOTA", {})
    return srv


class TestTheHistoryOffer:
    def test_the_first_refusal_leads_with_what_the_account_keeps(self, history_tool):
        first = history_tool._pro_api_call("show_branch_history", design_id="d1")
        assert first["code"] == "KILN_ACCOUNT_NOT_PAIRED"
        assert first["error"].startswith(signin_offer.HISTORY_OFFER)
        assert first["signin_offer"]["kind"] == "history"
        again = history_tool._pro_api_call("show_branch_history", design_id="d1")
        assert "signin_offer" not in again
        assert signin_offer.HISTORY_OFFER not in again["error"]
        assert again["code"] == "KILN_ACCOUNT_NOT_PAIRED", "the refusal itself always explains"

    def test_a_paid_history_tool_keeps_its_plan_and_offers_no_account(self, history_tool, monkeypatch):
        monkeypatch.setattr(history_tool, "_PRO_TOOL_TIERS", {"show_branch_history": "pro"})
        out = history_tool._pro_api_call("show_branch_history", design_id="d1")
        assert "signin_offer" not in out
        assert signin_offer.HISTORY_OFFER not in out["error"]

    def test_the_courier_offers_once_on_a_push_signed_out(self, history_tool):
        from kiln import history_courier

        first = history_courier._request("show_branch_history", "POST", "/api/cloud/x")
        assert first["signin_offer"]["kind"] == "history"
        assert first["error"].startswith(signin_offer.HISTORY_OFFER)
        assert "signin_offer" not in history_courier._request("show_branch_history", "POST", "/api/cloud/x")

    def test_the_courier_tells_a_lapsed_session_to_sign_back_in(self, history_tool, monkeypatch):
        from kiln import auth_session, history_courier

        monkeypatch.setattr(
            auth_session, "resolve_api_bearer",
            lambda *a, **k: auth_session.ApiBearer(
                token="", state="needs_signin", detail="Your Kiln session has expired. Sign back in.",
            ),
        )
        out = history_courier._request("show_branch_history", "POST", "/api/cloud/x")
        assert out["code"] == "KILN_SESSION_EXPIRED"
        assert "signin_offer" not in out and "free" not in out["error"].lower()


# ---------------------------------------------------------------------------
# The agent's half
# ---------------------------------------------------------------------------


def test_the_refusal_hint_names_the_tool_before_the_terminal_command():
    from kiln.tiers_and_terms import AGENT_SIGNIN_HINT

    assert AGENT_SIGNIN_HINT.index("`kiln_signin`") < AGENT_SIGNIN_HINT.index("`kiln signin`")


def test_get_started_names_the_kinds_already_offered(stats, monkeypatch):
    signin_offer.offer("design", "Made here.")
    assert signin_offer.offered_kinds() == ["design"]


# ---------------------------------------------------------------------------
# A print result recorded on this computer
# ---------------------------------------------------------------------------


@pytest.fixture
def outcome_tool(stats, tmp_path, monkeypatch):
    import kiln.persistence as _p
    import kiln.server as _srv
    from kiln.plugins import learning_tools as lt

    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setattr(_p, "_db", None, raising=False)
    monkeypatch.setattr(_srv, "_check_auth", lambda *_a, **_k: None)
    yield lambda job: lt.record_print_outcome(job_id=job, outcome="success", printer_name="p")
    monkeypatch.setattr(_p, "_db", None, raising=False)


class TestTheOutcomeOffer:
    def test_the_first_recorded_result_carries_the_offer_once(self, outcome_tool, stats):
        first = outcome_tool("j1")
        assert first["success"] is True
        assert first["signin_offer"]["kind"] == "outcome"
        assert "a design you keep in Kiln" in first["signin_offer"]["message"]
        assert "signin_offer" not in outcome_tool("j2")
        assert stats().get("offered_outcome") == 1

    def test_a_signed_in_install_records_with_no_offer(self, outcome_tool, tmp_path):
        _hold_session(tmp_path)
        assert "signin_offer" not in outcome_tool("j1")
