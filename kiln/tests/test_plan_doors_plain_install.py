"""The two plan doors on an install whose plan comes from the Kiln account.

On every ``pip install kiln3d`` the plan is the signed-in account's
(:mod:`kiln.account_plan`); nothing reads a license key file.  Until
2026-11-01 ``license_status`` crashed there ("cannot import name
'get_license_manager'"), and ``activate_license`` failed the same way.  Now
``license_status`` gives the answer ``check_my_tier`` gives, and
``activate_license`` says license keys are retired and to sign in, saving nothing.

The plain install is the public ``kiln.licensing`` without kiln-pro's
names, which is what this machine's kiln-pro would otherwise replace.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass

import pytest

import kiln.server as srv
from kiln.account_plan import LicenseTier
from kiln.auth_session import LICENSE_KEYS_RETIRED
from kiln.tiers_and_terms import SIGNIN_COMMAND


def _tool(fn):
    return getattr(fn, "fn", fn)


@dataclass
class _Session:
    token: str
    state: str
    detail: str = ""


@pytest.fixture
def plain(monkeypatch, tmp_path):
    """No kiln-pro licensing on this install, and its own ~/.kiln."""
    monkeypatch.setitem(sys.modules, "kiln.licensing", types.ModuleType("kiln.licensing"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
    tokens = tmp_path / ".kiln" / "auth_tokens.json"
    tokens.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("kiln.auth_session._tokens_path", lambda: tokens)
    return tokens


def _signed_in(monkeypatch, tokens, *, plan: LicenseTier, token: str = "jwt", state: str = "live"):
    tokens.write_text("{}")
    monkeypatch.setattr("kiln.auth_session.resolve_session_bearer", lambda: _Session(token, state, "Sign back in."))
    monkeypatch.setattr("kiln.account_plan.refresh_plan", lambda *a, **k: plan)


class TestLicenseStatus:
    def test_signed_out_is_free_and_says_how_to_sign_in(self, plain):
        answer = _tool(srv.license_status)()
        assert answer["success"] is True
        assert answer["tier"] == "free" and answer["source"] == "default"
        assert answer["is_valid"] is True
        assert answer["setup_hint"] == SIGNIN_COMMAND

    def test_signed_in_reads_the_accounts_plan(self, plain, monkeypatch):
        _signed_in(monkeypatch, plain, plan=LicenseTier.PRO)
        answer = _tool(srv.license_status)()
        assert answer == {
            "success": True,
            "tier": "pro",
            "is_valid": True,
            "is_expired": False,
            "source": "oauth",
            "agent_summary": answer["agent_summary"],
            "session_state": "live",
        }
        assert answer["agent_summary"].startswith("You're on the Pro tier.")

    def test_a_lapsed_sign_in_says_sign_back_in(self, plain, monkeypatch):
        _signed_in(monkeypatch, plain, plan=LicenseTier.FREE, token="", state="needs_signin")
        answer = _tool(srv.license_status)()
        assert answer["is_valid"] is False
        assert answer["action_required"] == "Sign back in."
        assert answer["setup_hint"] == SIGNIN_COMMAND

    def test_it_is_the_answer_check_my_tier_gives(self, plain, monkeypatch):
        from kiln.plugins.tier_diagnostic_tools import _account_plan_chain

        _signed_in(monkeypatch, plain, plan=LicenseTier.BUSINESS)
        assert _tool(srv.license_status)()["agent_summary"] == _account_plan_chain()["agent_summary"]


class TestActivateLicense:
    def test_a_key_is_not_saved_and_sign_in_is_the_way(self, plain, tmp_path):
        answer = _tool(srv.activate_license)(key="kiln_pro_abc123")
        assert answer["success"] is False and answer["code"] == "SIGN_IN_INSTEAD"
        assert answer["error"] == LICENSE_KEYS_RETIRED
        assert answer["setup_hint"] == SIGNIN_COMMAND
        assert not (tmp_path / ".kiln" / "license").exists()

    def test_an_empty_key_is_still_refused_as_input(self, plain):
        answer = _tool(srv.activate_license)(key="  ")
        assert answer["success"] is False
