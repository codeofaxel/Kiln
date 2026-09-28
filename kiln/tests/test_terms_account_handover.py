"""An agreement made on this machine reaches the account.

The hosted API refuses work until the ACCOUNT has agreed to the current
Terms.  ``record_acceptance`` mirrors an agreement to the account only when
the install already has a bearer, so a person who agreed first and signed in
second held a real agreement the account never received -- and was then
refused.  Two doors hand it over now: every sign-in door, the moment a bearer
exists, and the one chokepoint every hosted call goes through, when the
server refuses for want of it.  Neither ever agrees for anyone, and neither
turns an agreement to one version into an agreement to another.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from kiln import terms
from kiln.persistence import KilnDB
from kiln.terms import _CURRENT_TERMS_VERSION

# Bound at import, before the suite-wide fixture swaps ``terms.is_current`` for
# an always-true stand-in: the import-from-another-device case needs the real one.
from kiln.terms import is_current as _real_is_current


@pytest.fixture()
def db(tmp_path):
    return KilnDB(db_path=str(tmp_path / "test.db"))


@pytest.fixture(autouse=True)
def _no_account_bearer(monkeypatch, tmp_path):
    """No test reaches api.kiln3d.com: no license key, an empty sign-in home."""
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path / "no_auth_home"))


class _Account:
    """Stands in for the hosted terms endpoint: records what it was sent."""

    def __init__(self, answer: dict | None = None) -> None:
        self.sent: list[dict] = []
        self.answer = (
            {"accepted": True, "version": _CURRENT_TERMS_VERSION, "accepted_at": "t"}
            if answer is None
            else answer
        )

    def __call__(self, path, method, bearer, payload=None):
        self.sent.append({"path": path, "method": method, "bearer": bearer, "payload": payload})
        return self.answer


def _agree_before_sign_in(db, monkeypatch, *, method="cli", words="I accept"):
    """The person agrees while this install has no bearer: local record only."""
    offline = _Account()
    with monkeypatch.context() as before:
        before.setattr(terms, "_account_bearer", lambda: "")
        before.setattr(terms, "_server_request", offline)
        terms.record_acceptance(db=db, method=method, verbatim_text=words)
    assert offline.sent == []


# ---------------------------------------------------------------------------
# The hand-over itself
# ---------------------------------------------------------------------------


class TestSyncToAccount:
    def test_an_agreement_made_before_sign_in_is_handed_over_as_it_was_made(
        self, db, monkeypatch
    ):
        _agree_before_sign_in(db, monkeypatch, method="cli", words="I accept")
        account = _Account()
        monkeypatch.setattr(terms, "_server_request", account)

        assert terms.sync_to_account(db=db, bearer="jwt-new") is True
        assert account.sent == [{
            "path": "/api/terms/accept",
            "method": "POST",
            "bearer": "jwt-new",
            "payload": {
                "method": "cli",
                "verbatim_text": "I accept",
                "version": _CURRENT_TERMS_VERSION,
            },
        }]

    def test_nothing_is_sent_without_an_agreement_on_this_machine(self, db, monkeypatch):
        account = _Account()
        monkeypatch.setattr(terms, "_server_request", account)
        assert terms.sync_to_account(db=db, bearer="jwt") is False
        assert account.sent == []

    def test_an_agreement_to_one_version_is_never_sent_for_another(self, db, monkeypatch):
        _agree_before_sign_in(db, monkeypatch)
        account = _Account()
        monkeypatch.setattr(terms, "_server_request", account)
        assert terms.sync_to_account(db=db, bearer="jwt", version="99.0") is False
        assert account.sent == []

    def test_an_agreement_recorded_before_the_method_was_kept_says_unknown(
        self, db, monkeypatch
    ):
        # What an install that agreed on an earlier release holds: the
        # version and the time, and nothing about how.
        db.set_setting(terms._SETTINGS_KEY_VERSION, _CURRENT_TERMS_VERSION)
        db.set_setting(terms._SETTINGS_KEY_TIMESTAMP, "1700000000")
        account = _Account()
        monkeypatch.setattr(terms, "_server_request", account)

        assert terms.sync_to_account(db=db, bearer="jwt") is True
        assert account.sent[0]["payload"] == {
            "method": "unknown",
            "verbatim_text": None,
            "version": _CURRENT_TERMS_VERSION,
        }

    @pytest.mark.parametrize(
        "answer",
        [None, {"accepted": True, "version": "99.0"}, {"accepted": False}],
        ids=["no-answer", "another-version", "not-recorded"],
    )
    def test_only_a_confirmed_hand_over_counts(self, db, monkeypatch, answer):
        _agree_before_sign_in(db, monkeypatch)
        monkeypatch.setattr(terms, "_server_request", lambda *a, **k: answer)
        assert terms.sync_to_account(db=db, bearer="jwt") is False

    def test_an_agreement_imported_from_another_device_carries_no_method(
        self, db, monkeypatch
    ):
        """This install never saw how they agreed, so a method it holds from
        an agreement to an EARLIER version must not be replayed as this one's."""
        db.set_setting(terms._SETTINGS_KEY_VERSION, "0.9")
        db.set_setting(terms._SETTINGS_KEY_METHOD, "cli")
        db.set_setting(terms._SETTINGS_KEY_VERBATIM, "I accept")
        monkeypatch.setattr(terms, "_account_bearer", lambda: "jwt")
        monkeypatch.setattr(
            terms,
            "_server_request",
            _Account(answer={"accepted": True, "version": _CURRENT_TERMS_VERSION}),
        )
        assert _real_is_current(db=db, force_server=True) is True

        account = _Account()
        monkeypatch.setattr(terms, "_server_request", account)
        assert terms.sync_to_account(db=db, bearer="jwt") is True
        assert account.sent[0]["payload"]["method"] == "unknown"
        assert account.sent[0]["payload"]["verbatim_text"] is None


# ---------------------------------------------------------------------------
# The chokepoint every hosted call goes through
# ---------------------------------------------------------------------------


def _terms_refusal(url: str, version: str = _CURRENT_TERMS_VERSION) -> urllib.error.HTTPError:
    body = {
        "error": "terms_required",
        "version": version,
        "message": "Before Kiln can help, please agree to Kiln's Terms of Use: https://kiln3d.com/accept/t",
        "accept_url": "https://kiln3d.com/accept/t",
    }
    return urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(json.dumps(body).encode()))


class _Answer:
    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestTheHostedCallHandsItOver:
    """``server._pro_api_call`` answers a missing-agreement refusal once."""

    @pytest.fixture()
    def hosted(self, monkeypatch, db):
        monkeypatch.setenv("KILN_LICENSE_KEY", "lk-test")
        monkeypatch.setenv("KILN_API_URL", "https://api.test")
        monkeypatch.setattr("kiln.persistence.get_db", lambda: db)
        calls: list[str] = []
        script: list = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            step = script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        return calls, script

    def test_an_install_that_agreed_is_served_after_handing_it_over(
        self, hosted, db, monkeypatch
    ):
        from kiln import server

        calls, script = hosted
        _agree_before_sign_in(db, monkeypatch)
        account = _Account()
        monkeypatch.setattr(terms, "_server_request", account)
        url = "https://api.test/api/tools/generate_coaster"
        script += [_terms_refusal(url), _Answer({"status": "ok", "made": "coaster"})]

        answer = server._pro_api_call("generate_coaster", size_mm=80)

        assert answer == {"status": "ok", "made": "coaster"}
        assert calls == [url, url]
        # Handed over with the very bearer the refused call carried.
        assert [s["bearer"] for s in account.sent] == ["lk-test"]
        assert account.sent[0]["payload"]["version"] == _CURRENT_TERMS_VERSION

    def test_an_install_that_never_agreed_gets_the_refusal_and_its_link(
        self, hosted, monkeypatch
    ):
        from kiln import server

        calls, script = hosted
        account = _Account()
        monkeypatch.setattr(terms, "_server_request", account)
        url = "https://api.test/api/tools/generate_coaster"
        script.append(_terms_refusal(url))

        answer = server._pro_api_call("generate_coaster", size_mm=80)

        assert answer["error"] == "terms_required"
        assert answer["accept_url"] == "https://kiln3d.com/accept/t"
        assert calls == [url]
        assert account.sent == []

    def test_a_refusal_for_another_version_is_left_for_the_person(
        self, hosted, db, monkeypatch
    ):
        from kiln import server

        calls, script = hosted
        _agree_before_sign_in(db, monkeypatch)
        account = _Account()
        monkeypatch.setattr(terms, "_server_request", account)
        url = "https://api.test/api/tools/generate_coaster"
        script.append(_terms_refusal(url, version="99.0"))

        answer = server._pro_api_call("generate_coaster", size_mm=80)

        assert answer["error"] == "terms_required"
        assert calls == [url]
        assert account.sent == []

    def test_it_asks_once_more_and_never_loops(self, hosted, db, monkeypatch):
        from kiln import server

        calls, script = hosted
        _agree_before_sign_in(db, monkeypatch)
        monkeypatch.setattr(terms, "_server_request", _Account())
        url = "https://api.test/api/tools/generate_coaster"
        script += [_terms_refusal(url), _terms_refusal(url)]

        answer = server._pro_api_call("generate_coaster", size_mm=80)

        assert answer["error"] == "terms_required"
        assert calls == [url, url]


# ---------------------------------------------------------------------------
# Every sign-in door hands it over the moment a bearer exists
# ---------------------------------------------------------------------------


@pytest.fixture()
def handed(monkeypatch, tmp_path):
    """Record every hand-over; sign-in writes land in a temp home."""
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    seen: list[str] = []

    def fake_sync(*, db=None, bearer=None, version=None):
        seen.append(bearer)
        return True

    monkeypatch.setattr(terms, "sync_to_account", fake_sync)
    return seen


_SIGNED_IN = {
    "status": "success",
    "success": True,
    "access_token": "tok-signed-in",
    "refresh_token": "ref",
    "email": "a@example.com",
    "tier": "free",
    "auth_uid": "uid-1",
    "has_entitlement": False,
}


def test_kiln_signin_hands_it_over(handed, monkeypatch):
    import click
    from click.testing import CliRunner

    from kiln.cli import auth_commands

    def fake_post(path, payload, *, bearer=None, timeout=15.0):
        if path.endswith("/device/start"):
            return {
                "success": True,
                "device_code": "dev-1",
                "user_code": "WXYZ-1234",
                "verification_uri": "https://app.kiln3d.com/auth/device",
                "interval": 1,
                "expires_in": 900,
            }
        return dict(_SIGNED_IN)

    monkeypatch.setattr(auth_commands, "_http_post", fake_post)
    monkeypatch.setattr(auth_commands.webbrowser, "open", lambda *a, **k: False)
    g = click.Group("kiln")
    auth_commands.register_auth_cli(g)
    result = CliRunner().invoke(g, ["login", "--no-browser"])
    assert result.exit_code == 0, result.output
    assert handed == ["tok-signed-in"]


def test_kiln_pair_hands_it_over(handed, monkeypatch):
    import click
    from click.testing import CliRunner

    from kiln.cli import auth_commands

    monkeypatch.setattr(auth_commands, "_http_post", lambda *a, **k: dict(_SIGNED_IN))
    monkeypatch.setattr(auth_commands, "_http_get", lambda *a, **k: (200, {}))
    g = click.Group("kiln")
    auth_commands.register_auth_cli(g)
    result = CliRunner().invoke(g, ["pair", "KLN-ABCD-EFGH", "--client", "Terminal"])
    assert result.exit_code == 0, result.output
    assert handed == ["tok-signed-in"]


def test_the_in_chat_sign_in_hands_it_over(handed, monkeypatch):
    import kiln.cli.auth_commands as ac
    import kiln.plugins.auth_tools as at
    from kiln.mcp_compat import FastMCP

    monkeypatch.setattr(ac, "_http_post", lambda p, b=None, **k: dict(_SIGNED_IN))
    m = FastMCP("t")
    at.register(m)
    poll = {t.name: t.fn for t in m._tool_manager.list_tools()}["kiln_signin_poll"]
    assert poll(device_code="dc-1")["status"] == "success"
    assert handed == ["tok-signed-in"]


def test_a_failed_hand_over_never_fails_the_sign_in(monkeypatch, tmp_path):
    import kiln.cli.auth_commands as ac
    import kiln.plugins.auth_tools as at
    from kiln.mcp_compat import FastMCP

    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))

    def broken(**_kw):
        raise RuntimeError("store down")

    monkeypatch.setattr(terms, "sync_to_account", broken)
    monkeypatch.setattr(ac, "_http_post", lambda p, b=None, **k: dict(_SIGNED_IN))
    m = FastMCP("t")
    at.register(m)
    poll = {t.name: t.fn for t in m._tool_manager.list_tools()}["kiln_signin_poll"]
    assert poll(device_code="dc-1")["status"] == "success"
    assert (tmp_path / ".kiln" / "auth_tokens.json").exists()
