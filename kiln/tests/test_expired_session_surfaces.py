"""Every status surface says the same thing about a sign-in that ran out.

2026-09-30: a session whose refresh the server had rejected three days
earlier read as ``signed_in: true`` in get_started and as Enterprise with
"everything is unlocked" in check_my_tier, while the browser-link path said
"Kiln is signed out".  One person, one state, three answers.  The lapsed
owner is neither a stranger (no account nudge) nor all-clear: every surface
names the lapse and says to sign back in.
"""

from __future__ import annotations

import base64
import json
import struct
import time
from unittest.mock import MagicMock

import pytest

import kiln.auth_session as auth_session
from kiln import stage_link
from kiln.served_answer import Miss, clause, sentence


def _jwt(exp: float) -> str:
    seg = lambda d: base64.urlsafe_b64encode(  # noqa: E731
        json.dumps(d).encode()
    ).rstrip(b"=").decode()
    return f"{seg({'alg': 'none'})}.{seg({'exp': exp})}.sig"


@pytest.fixture()
def lapsed_session(tmp_path, monkeypatch):
    """The token file a rejected refresh leaves behind: no refresh token,
    the rejection stamped, the access token days past its expiry."""
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    monkeypatch.setattr(auth_session, "_last_network_failure_monotonic", None)
    kdir = tmp_path / ".kiln"
    kdir.mkdir()
    (kdir / "auth_tokens.json").write_text(json.dumps({
        "access_token": _jwt(time.time() - 3 * 86400),
        "email": "owner@example.com",
        "tier": "enterprise",
        "signed_in_at": int(time.time()),
        "refresh_rejected_at": "2026-09-28T04:38:08Z",
    }))

    def _no_exchange(*_a, **_k):
        raise AssertionError("a rejected session must not be re-exchanged")

    monkeypatch.setattr(auth_session, "_post_refresh", _no_exchange)
    return tmp_path


def test_served_doors_word_an_expired_session_as_sign_back_in():
    expired = Miss("signed_out", "KILN_SESSION_EXPIRED")  # the wire code
    text = sentence(
        expired, feature="servers", on_the_line="The tool asks Kiln's servers",
        cannot="run it", wont="has no answer",
    )
    assert "your Kiln session has expired" in text
    assert "Sign back in and try again." in text
    assert "(your Kiln session has expired); sign back in" in clause(
        expired, feature="servers", cannot="issue a browser link"
    )
    # Never having signed in is still worded as it always was.
    stranger = sentence(
        Miss("signed_out", "KILN_ACCOUNT_NOT_PAIRED"), feature="servers",
        on_the_line="The tool asks Kiln's servers", cannot="run it", wont="has no answer",
    )
    assert "Kiln is signed out" in stranger and "Sign in and try again." in stranger


def test_the_browser_link_says_the_session_expired(lapsed_session, monkeypatch):
    stage_link._cache.clear()
    monkeypatch.delenv(stage_link._OPT_OUT_ENV, raising=False)
    recorded: list[str] = []
    monkeypatch.setattr(stage_link, "_refused", lambda _p, reason, _e: recorded.append(reason))
    mesh = lapsed_session / "part.stl"
    tri = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, time.time() % 1, 0, 0, 0, 1, 0, 0)
    mesh.write_bytes(b"\x00" * 80 + struct.pack("<I", 1) + tri)

    assert stage_link.stage_link_for(mesh) is None
    assert recorded == ["session_expired"]
    assert "your Kiln session has expired" in stage_link.refusal_sentence(recorded[0])


def test_get_started_names_the_lapse_and_not_an_account_nudge(lapsed_session):
    import kiln.server as srv

    get_started = next(
        t.fn for t in srv.mcp._tool_manager.list_tools() if t.name == "get_started"
    )
    account = get_started()["account"]

    assert account["signed_in"] is True  # an owner, not a stranger
    assert account["session_state"] == "needs_signin"
    assert "has expired" in account["action_required"]
    assert account["setup_hint"] == "kiln signin"
    assert "tool" not in account  # the free-account nudge is for strangers


def test_the_refusal_verdict_reads_from_the_file_alone(lapsed_session):
    assert auth_session.session_rejected() is True
    tokens = json.loads((lapsed_session / ".kiln" / "auth_tokens.json").read_text())
    tokens["refresh_token"] = "r"  # a fresh sign-in carries one again
    assert auth_session.session_rejected(tokens) is False


def _manager(monkeypatch, tier: str, source: str):
    lic = pytest.importorskip("kiln_pro.enterprise.licensing")
    mgr = MagicMock()
    mgr.get_tier.return_value = lic.LicenseTier(tier)
    mgr.get_info.return_value.source = source
    mgr.get_info.return_value.to_dict.return_value = {
        "tier": tier, "source": source, "is_valid": True,
    }
    monkeypatch.setattr(lic, "get_license_manager", lambda: mgr)
    return mgr


def test_check_my_tier_says_a_lapsed_plan_comes_back_on_sign_in(lapsed_session, monkeypatch):
    """A session the server refused grants no paid tier, so this machine
    reads free -- and its owner hears that, not an invitation to upgrade."""
    from kiln.plugins.tier_diagnostic_tools import _walk_resolution_chain

    _manager(monkeypatch, "free", "default")
    out = _walk_resolution_chain()

    assert out["effective_tier"] == "free"
    assert out["session_state"] == "needs_signin"
    assert out["agent_summary"].startswith("Your Kiln session")
    assert "your Enterprise plan comes back the moment you sign back in" in out["agent_summary"]
    assert out["action_required"] == auth_session.resolve_session_bearer().detail
    assert out["setup_hint"] == "kiln signin"


def test_check_my_tier_still_leads_with_the_lapse_while_a_plan_is_granted(
    lapsed_session, monkeypatch
):
    """An older licence manager still grants the cached plan; the lapse
    leads the answer all the same."""
    from kiln.plugins.tier_diagnostic_tools import _walk_resolution_chain

    _manager(monkeypatch, "enterprise", "oauth")
    out = _walk_resolution_chain()

    assert out["effective_tier"] == "enterprise"
    assert out["agent_summary"].startswith("Your Kiln session")
    assert "Everything is unlocked" not in out["agent_summary"]


def test_license_status_explains_a_free_answer_a_lapsed_session_caused(
    lapsed_session, monkeypatch
):
    import kiln.server as srv

    _manager(monkeypatch, "free", "default")
    payload = srv.license_status()

    assert payload["tier"] == "free"
    assert payload["session_state"] == "needs_signin"
    assert payload["is_valid"] is False
    assert "has expired" in payload["action_required"]


def test_an_operator_key_reports_exactly_what_it_did_before(lapsed_session, monkeypatch):
    import kiln.server as srv

    _manager(monkeypatch, "pro", "env")
    payload = srv.license_status()

    assert "session_state" not in payload and "action_required" not in payload
