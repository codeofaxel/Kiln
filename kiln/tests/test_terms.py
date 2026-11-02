"""Tests for kiln.terms -- terms of use acceptance tracking."""

from __future__ import annotations

import time
from unittest import mock

import pytest

from kiln.persistence import KilnDB
from kiln.terms import (
    _CURRENT_TERMS_VERSION,
    _SETTINGS_KEY_TIMESTAMP,
    _SETTINGS_KEY_VERSION,
    _SUMMARY_REVIEWED_FOR_VERSION,
    _TERMS_SUMMARY,
    get_accepted_version,
    is_current,
    prompt_acceptance,
    record_acceptance,
)


@pytest.fixture()
def db(tmp_path):
    """In-memory-like DB using a temp file."""
    return KilnDB(db_path=str(tmp_path / "test.db"))


@pytest.fixture(autouse=True)
def _no_account_bearer(monkeypatch, tmp_path):
    """Keep every terms test pure-local by default.

    ``is_current`` / ``record_acceptance`` consult the account only when a bearer
    resolves (``KILN_LICENSE_KEY`` or a paired token under ``$KILN_AUTH_HOME``).
    Clear the license env and point ``KILN_AUTH_HOME`` at an empty temp dir so no
    test accidentally reaches ``api.kiln3d.com``; the account path is exercised
    explicitly (via ``mock``) in ``TestAccountScopedAcceptance``.
    """
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path / "no_auth_home"))


# ---------------------------------------------------------------------------
# get_accepted_version
# ---------------------------------------------------------------------------


class TestGetAcceptedVersion:
    def test_returns_none_when_never_accepted(self, db):
        assert get_accepted_version(db=db) is None

    def test_returns_version_after_acceptance(self, db):
        db.set_setting(_SETTINGS_KEY_VERSION, "1.0")
        assert get_accepted_version(db=db) == "1.0"

    def test_returns_stale_version(self, db):
        db.set_setting(_SETTINGS_KEY_VERSION, "0.9")
        assert get_accepted_version(db=db) == "0.9"


# ---------------------------------------------------------------------------
# is_current
# ---------------------------------------------------------------------------


class TestIsCurrent:
    def test_false_when_never_accepted(self, db):
        assert is_current(db=db) is False

    def test_false_when_old_version(self, db):
        db.set_setting(_SETTINGS_KEY_VERSION, "0.1")
        assert is_current(db=db) is False

    def test_true_when_current_version(self, db):
        db.set_setting(_SETTINGS_KEY_VERSION, _CURRENT_TERMS_VERSION)
        assert is_current(db=db) is True


# ---------------------------------------------------------------------------
# record_acceptance
# ---------------------------------------------------------------------------


class TestRecordAcceptance:
    def test_stores_version_and_timestamp(self, db):
        before = time.time()
        record_acceptance(db=db)
        after = time.time()

        assert db.get_setting(_SETTINGS_KEY_VERSION) == _CURRENT_TERMS_VERSION
        ts = float(db.get_setting(_SETTINGS_KEY_TIMESTAMP))
        assert before <= ts <= after

    def test_is_current_after_acceptance(self, db):
        assert is_current(db=db) is False
        record_acceptance(db=db)
        assert is_current(db=db) is True

    def test_overwrite_old_version(self, db):
        db.set_setting(_SETTINGS_KEY_VERSION, "0.1")
        assert is_current(db=db) is False
        record_acceptance(db=db)
        assert is_current(db=db) is True


# ---------------------------------------------------------------------------
# prompt_acceptance
# ---------------------------------------------------------------------------


class TestPromptAcceptance:
    def test_returns_true_when_phrase_typed(self, db):
        with mock.patch("kiln.persistence.get_db", return_value=db):
            with mock.patch("click.prompt", return_value="I accept"):
                with mock.patch("click.echo"):
                    assert prompt_acceptance() is True
        assert is_current(db=db) is True

    def test_accept_is_case_and_whitespace_forgiving(self, db):
        with mock.patch("kiln.persistence.get_db", return_value=db):
            with mock.patch("click.prompt", return_value="  i  ACCEPT.  "):
                with mock.patch("click.echo"):
                    assert prompt_acceptance() is True
        assert is_current(db=db) is True

    def test_returns_false_on_anything_else(self, db):
        # A bare "yes" no longer accepts — only the deliberate phrase does.
        with mock.patch("kiln.persistence.get_db", return_value=db):
            with mock.patch("click.prompt", return_value="yes"):
                with mock.patch("click.echo"):
                    assert prompt_acceptance() is False
        assert is_current(db=db) is False

    def test_empty_input_does_not_accept(self, db):
        # A bare Enter must NOT accept — consent is a typed act, not a default
        # keypress (this was the weak spot: the old confirm defaulted to yes).
        with mock.patch("kiln.persistence.get_db", return_value=db):
            with mock.patch("click.prompt", return_value=""):
                with mock.patch("click.echo"):
                    assert prompt_acceptance() is False
        assert get_accepted_version(db=db) is None

    def test_records_verbatim_consent_text(self, db):
        # The exact phrase typed is stored as the consent proof (parity with the
        # in-chat MCP path's verbatim_text).
        with mock.patch("kiln.persistence.get_db", return_value=db):
            with mock.patch("click.prompt", return_value="I accept"):
                with mock.patch("click.echo"):
                    with mock.patch("kiln.terms.record_acceptance") as rec:
                        assert prompt_acceptance(method="cli") is True
        assert rec.call_args.kwargs.get("verbatim_text") == "I accept"
        assert rec.call_args.kwargs.get("method") == "cli"


class TestReview:
    def test_shows_summary_and_when_accepted(self, db, capsys):
        from kiln.terms import review

        record_acceptance(db=db)
        review(db=db)
        out = capsys.readouterr().out
        assert "Terms of Use" in out                  # the summary is re-shown
        assert "accepted these terms" in out.lower()  # + when it was accepted

    def test_shows_not_accepted_when_absent(self, db, capsys):
        from kiln.terms import review

        review(db=db)
        assert "not accepted" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# Summary review marker -- forcing function so the copy can't silently go stale
# ---------------------------------------------------------------------------


class TestSummaryReviewMarker:
    def test_summary_reviewed_for_current_version(self):
        """The review marker must track the current terms version.

        Goes red whenever _CURRENT_TERMS_VERSION is bumped without
        re-reviewing _TERMS_SUMMARY and the other acceptance surfaces.
        Bumping the marker is the conscious "yes, I refreshed the copy" step.
        """
        assert _SUMMARY_REVIEWED_FOR_VERSION == _CURRENT_TERMS_VERSION, (
            "Terms version changed but the summary review marker was not bumped. "
            "Re-read _TERMS_SUMMARY and the web/MCP acceptance copy, update what "
            "materially changed, then set _SUMMARY_REVIEWED_FOR_VERSION to match."
        )

    def test_summary_covers_the_load_bearing_points(self):
        """The summary must carry the points that make it fair notice."""
        # Safety responsibility
        assert "safety" in _TERMS_SUMMARY.lower()
        # The tier / commercial rule (the headline v3.0 addition)
        assert "Business" in _TERMS_SUMMARY
        # Fee transparency
        assert "5%" in _TERMS_SUMMARY
        # Canonical links
        assert "https://kiln3d.com/terms" in _TERMS_SUMMARY
        assert "https://kiln3d.com/privacy" in _TERMS_SUMMARY


# ---------------------------------------------------------------------------
# Account-scoped acceptance — honored across the user's devices
# ---------------------------------------------------------------------------


class TestAccountScopedAcceptance:
    def test_local_current_short_circuits_no_server(self, db, monkeypatch):
        """A current local record is authoritative — the server is never consulted."""
        record_acceptance(db=db)  # no bearer (autouse) -> local only
        called = {"n": 0}
        monkeypatch.setattr("kiln.terms._account_bearer", lambda: "lic-irrelevant")
        monkeypatch.setattr(
            "kiln.terms._server_request",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1),
        )
        assert is_current(db=db) is True
        assert called["n"] == 0  # short-circuits before any bearer / network

    def test_no_bearer_is_local_only(self, db, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr("kiln.terms._account_bearer", lambda: "")
        monkeypatch.setattr(
            "kiln.terms._server_request",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1),
        )
        assert is_current(db=db) is False
        assert called["n"] == 0

    def test_cross_device_acceptance_imported_and_backfilled(self, db, monkeypatch):
        """Stale local + bearer + server says accepted -> True, and local backfills."""
        monkeypatch.setattr("kiln.terms._account_bearer", lambda: "lic-paid")
        monkeypatch.setattr(
            "kiln.terms._server_request",
            lambda path, method, bearer, payload=None: {
                "accepted": True,
                "version": _CURRENT_TERMS_VERSION,
                "accepted_at": "t0",
            },
        )
        assert get_accepted_version(db=db) is None
        assert is_current(db=db) is True
        # backfilled so the next check is fast + offline-safe
        assert get_accepted_version(db=db) == _CURRENT_TERMS_VERSION

    def test_server_says_not_accepted_stays_false(self, db, monkeypatch):
        monkeypatch.setattr("kiln.terms._account_bearer", lambda: "lic-paid")
        monkeypatch.setattr(
            "kiln.terms._server_request",
            lambda *a, **k: {"accepted": False, "version": _CURRENT_TERMS_VERSION},
        )
        assert is_current(db=db) is False
        assert get_accepted_version(db=db) is None  # nothing backfilled

    def test_server_unreachable_is_false_not_raise(self, db, monkeypatch):
        monkeypatch.setattr("kiln.terms._account_bearer", lambda: "lic-paid")
        monkeypatch.setattr("kiln.terms._server_request", lambda *a, **k: None)
        assert is_current(db=db) is False

    # --- silence against an answer: the network down vs the server saying no

    @staticmethod
    def _account_on_disk(monkeypatch, tmp_path):
        """A token file holding a session the install would renew: an identity."""
        import json

        (tmp_path / ".kiln").mkdir()
        (tmp_path / ".kiln" / "auth_tokens.json").write_text(json.dumps({
            "email": "maker@example.com",
            "access_token": "some.jwt.token",
            "refresh_token": "still-good",
        }))
        monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))

    def test_is_current_offline_with_account_defers(self, db, monkeypatch, tmp_path):
        """Nothing answered and an account is on this machine: the gate is
        told so (and runs the command), and no throttle stamp is written, so
        the next command asks again rather than waiting out the window."""
        import urllib.error
        import urllib.request

        from kiln.terms import _SETTINGS_KEY_SERVER_CHECK, TermsCheckUnreachable

        self._account_on_disk(monkeypatch, tmp_path)
        monkeypatch.setattr("kiln.terms._account_bearer", lambda: "some.jwt.token")

        def _no_network(*a, **k):
            raise urllib.error.URLError("Name or service not known")

        monkeypatch.setattr(urllib.request, "urlopen", _no_network)
        with pytest.raises(TermsCheckUnreachable):
            is_current(db=db)
        assert db.get_setting(_SETTINGS_KEY_SERVER_CHECK) is None
        assert get_accepted_version(db=db) is None

    def test_is_current_refused_with_account_is_false(self, db, monkeypatch, tmp_path):
        """The server answered, and the answer was no: the Terms are shown,
        and the throttle stamp is written as for any other answer."""
        import urllib.error
        import urllib.request

        from kiln.terms import _SETTINGS_KEY_SERVER_CHECK

        self._account_on_disk(monkeypatch, tmp_path)
        monkeypatch.setattr("kiln.terms._account_bearer", lambda: "some.jwt.token")

        def _refused(*a, **k):
            raise urllib.error.HTTPError("https://api.kiln3d.com/api/terms/acceptance", 401, "Unauthorized", {}, None)

        monkeypatch.setattr(urllib.request, "urlopen", _refused)
        assert is_current(db=db) is False
        assert db.get_setting(_SETTINGS_KEY_SERVER_CHECK) is not None

    def test_is_current_offline_without_account_is_false(self, db, monkeypatch):
        """A bearer with no account identity on disk (a licence key alone)
        never fails open: the local record is its only gate."""
        import urllib.error
        import urllib.request

        from kiln.terms import _SETTINGS_KEY_SERVER_CHECK

        monkeypatch.setattr("kiln.terms._account_bearer", lambda: "lic-paid")

        def _no_network(*a, **k):
            raise urllib.error.URLError("Name or service not known")

        monkeypatch.setattr(urllib.request, "urlopen", _no_network)
        assert is_current(db=db) is False
        assert db.get_setting(_SETTINGS_KEY_SERVER_CHECK) is None

    def test_a_second_check_within_the_backoff_makes_no_request(self, db, monkeypatch, tmp_path):
        """An offline machine runs many gated commands; the account is
        asked once a minute, not once a command, and the memory is on disk
        so every Kiln process shares it."""
        import urllib.error
        import urllib.request

        from kiln.terms import (
            _SETTINGS_KEY_SERVER_CHECK,
            _SETTINGS_KEY_SERVER_UNREACHABLE,
            TermsCheckUnreachable,
        )

        self._account_on_disk(monkeypatch, tmp_path)
        monkeypatch.setattr("kiln.terms._account_bearer", lambda: "some.jwt.token")
        asked = {"n": 0}

        def _no_network(*a, **k):
            asked["n"] += 1
            raise urllib.error.URLError("Name or service not known")

        monkeypatch.setattr(urllib.request, "urlopen", _no_network)
        with pytest.raises(TermsCheckUnreachable):
            is_current(db=db)
        with pytest.raises(TermsCheckUnreachable):
            is_current(db=db)
        assert asked["n"] == 1
        assert db.get_setting(_SETTINGS_KEY_SERVER_UNREACHABLE) is not None
        assert db.get_setting(_SETTINGS_KEY_SERVER_CHECK) is None
        # Once the memory lapses the account is asked again.
        db.set_setting(_SETTINGS_KEY_SERVER_UNREACHABLE, str(time.time() - 61))
        with pytest.raises(TermsCheckUnreachable):
            is_current(db=db)
        assert asked["n"] == 2

    def test_the_backoff_answers_false_for_no_account_without_a_request(self, db, monkeypatch):
        import urllib.error
        import urllib.request

        monkeypatch.setattr("kiln.terms._account_bearer", lambda: "lic-paid")
        asked = {"n": 0}

        def _no_network(*a, **k):
            asked["n"] += 1
            raise urllib.error.URLError("down")

        monkeypatch.setattr(urllib.request, "urlopen", _no_network)
        assert is_current(db=db) is False
        assert is_current(db=db) is False
        assert asked["n"] == 1

    def test_record_acceptance_offline_keeps_the_local_record(self, db, monkeypatch):
        import urllib.error
        import urllib.request

        monkeypatch.setattr("kiln.terms._account_bearer", lambda: "some.jwt.token")

        def _no_network(*a, **k):
            raise urllib.error.URLError("down")

        monkeypatch.setattr(urllib.request, "urlopen", _no_network)
        record_acceptance(db=db, method="cli")
        assert get_accepted_version(db=db) == _CURRENT_TERMS_VERSION

    def test_lapsed_account_signin_is_empty_when_never_signed_in(self):
        from kiln.terms import lapsed_account_signin

        assert lapsed_account_signin() == ""

    def test_lapsed_account_signin_names_the_refused_session(self, monkeypatch, tmp_path):
        """A stale local record on a machine whose sign-in the server refused
        is not "never accepted": the account may hold the agreement, and the
        gate must say so in the words every other door uses."""
        import json

        from kiln.terms import lapsed_account_signin

        (tmp_path / ".kiln").mkdir()
        (tmp_path / ".kiln" / "auth_tokens.json").write_text(json.dumps({
            "email": "maker@example.com",
            "access_token": "expired.jwt.token",
            "refresh_rejected_at": "2026-10-19T02:36:31Z",
        }))
        monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))

        message = lapsed_account_signin()

        assert "maker@example.com" in message
        assert "expired" in message.lower()

    def test_lapsed_account_signin_is_empty_while_the_session_can_renew(
        self, monkeypatch, tmp_path,
    ):
        import json

        from kiln.terms import lapsed_account_signin

        (tmp_path / ".kiln").mkdir()
        (tmp_path / ".kiln" / "auth_tokens.json").write_text(json.dumps({
            "email": "maker@example.com",
            "access_token": "some.jwt.token",
            "refresh_token": "still-good",
        }))
        monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))

        assert lapsed_account_signin() == ""

    def test_recheck_is_throttled(self, db, monkeypatch):
        """A second is_current within the TTL does not re-hit the server."""
        monkeypatch.setattr("kiln.terms._account_bearer", lambda: "lic-paid")
        calls = {"n": 0}

        def _srv(*a, **k):
            calls["n"] += 1
            return {"accepted": False, "version": _CURRENT_TERMS_VERSION}

        monkeypatch.setattr("kiln.terms._server_request", _srv)
        assert is_current(db=db) is False
        assert is_current(db=db) is False  # throttled
        assert calls["n"] == 1

    def test_record_posts_to_account_when_bearer_present(self, db, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr("kiln.terms._account_bearer", lambda: "lic-paid")

        def _srv(path, method, bearer, payload=None):
            captured.update(path=path, method=method, bearer=bearer, payload=payload)
            return {"accepted": True, "accepted_at": "t0"}

        monkeypatch.setattr("kiln.terms._server_request", _srv)
        record_acceptance(db=db, method="mcp_in_chat", verbatim_text="I accept the Kiln Terms")
        assert get_accepted_version(db=db) == _CURRENT_TERMS_VERSION  # local written
        assert captured["path"] == "/api/terms/accept"
        assert captured["method"] == "POST"
        assert captured["bearer"] == "lic-paid"
        assert captured["payload"] == {
            "method": "mcp_in_chat",
            "verbatim_text": "I accept the Kiln Terms",
            # The server holds the record to the version the person agreed to.
            "version": _CURRENT_TERMS_VERSION,
        }

    def test_record_local_only_without_bearer(self, db, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr("kiln.terms._account_bearer", lambda: "")
        monkeypatch.setattr(
            "kiln.terms._server_request",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1),
        )
        record_acceptance(db=db, method="cli")
        assert get_accepted_version(db=db) == _CURRENT_TERMS_VERSION
        assert called["n"] == 0
