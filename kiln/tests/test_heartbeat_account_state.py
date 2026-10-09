"""Whether an install is signed in, as one word in the daily heartbeat.

The server only ever sees a signed-in install when it makes a hosted
request, so an install that signs in and then slices and prints locally
reads as "no account" from the outside.  The heartbeat carries the state
as a word from a closed vocabulary -- never which account -- and reads it
from the session file alone, so a heartbeat never costs a sign-in exchange.
"""

from __future__ import annotations

import json

import pytest

from kiln import auth_session, heartbeat


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    return tmp_path


def _write(home, data: dict) -> None:
    path = home / ".kiln" / "auth_tokens.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class TestTheWord:
    def test_no_session_file_is_signed_out(self, home):
        assert heartbeat._account_state() == "signed_out"

    def test_a_stored_session_is_signed_in_even_with_a_lapsed_access_token(self, home):
        # No clock judgement: a lapsed token refreshes on the next call, so
        # the person is still signed in.  No network: nothing is exchanged.
        _write(home, {"access_token": "expired.jwt.here", "refresh_token": "r"})
        assert heartbeat._account_state() == "signed_in"

    def test_a_refresh_token_alone_is_still_signed_in(self, home):
        _write(home, {"refresh_token": "r"})
        assert heartbeat._account_state() == "signed_in"

    def test_a_session_the_server_refused_is_needs_signin(self, home, monkeypatch):
        _write(home, {"access_token": "a", "refresh_token": "r"})
        monkeypatch.setattr(auth_session, "session_rejected", lambda stored=None: True)
        assert heartbeat._account_state() == "needs_signin"

    def test_an_operator_licence_key_wins_and_is_not_a_sign_in(self, home, monkeypatch):
        _write(home, {"access_token": "a", "refresh_token": "r"})
        monkeypatch.setenv("KILN_LICENSE_KEY", "kiln-licence-xyz")
        assert heartbeat._account_state() == "license"

    def test_an_empty_session_file_is_signed_out(self, home):
        _write(home, {"access_token": "", "refresh_token": ""})
        assert heartbeat._account_state() == "signed_out"

    def test_a_broken_read_is_unknown_never_a_guess(self, home, monkeypatch):
        monkeypatch.setattr(
            auth_session, "_read_tokens", lambda: (_ for _ in ()).throw(RuntimeError("disk"))
        )
        assert heartbeat._account_state() == "unknown"

    def test_every_answer_is_in_the_closed_vocabulary(self, home, monkeypatch):
        seen = {heartbeat._account_state()}
        _write(home, {"refresh_token": "r"})
        seen.add(heartbeat._account_state())
        monkeypatch.setenv("KILN_LICENSE_KEY", "k")
        seen.add(heartbeat._account_state())
        assert seen <= set(heartbeat._ACCOUNT_STATES)


class TestItRidesTheHeartbeat:
    def test_the_payload_carries_the_word_and_never_a_token(self, home, monkeypatch):
        _write(home, {"access_token": "secret-access", "refresh_token": "secret-refresh"})
        import urllib.request

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
        assert beats[0]["p_details"]["account"] == "signed_in"
        assert "secret" not in json.dumps(sent)
