"""Tests for :mod:`kiln.auth_session` — the live paired-session resolver.

Covers every state in the resolver's contract: the no-network fast path,
the refresh exchange (rotation persisted, sibling fields preserved,
0600 perms), the rejected-refresh path (file left intact — never
destructive), the unreachable-endpoint degradation with backoff, the
lock's actual mutual exclusion, the post-lock re-read when a rival
process refreshes first, and the "returns a state, never raises"
contract.  All network I/O is monkeypatched; no test talks to a server.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import time

import pytest

import kiln.auth_session as auth_session
from kiln.auth_session import (
    SessionBearer,
    get_paired_access_token,
    resolve_session_bearer,
)


def _jwt(exp: float) -> str:
    """Minimal unsigned JWT with the given ``exp`` claim."""
    seg = lambda d: base64.urlsafe_b64encode(  # noqa: E731
        json.dumps(d).encode()
    ).rstrip(b"=").decode()
    return f"{seg({'alg': 'none'})}.{seg({'exp': exp})}.sig"


@pytest.fixture()
def auth_home(tmp_path, monkeypatch):
    """Redirect the token file into tmp and reset module backoff state."""
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.setattr(auth_session, "_last_network_failure_monotonic", None)
    return tmp_path


def _write_session(auth_home, **overrides) -> dict:
    data = {
        "access_token": _jwt(time.time() + 3600),
        "refresh_token": "rt-original",
        "email": "user@example.com",
        "tier": "enterprise",
        "auth_uid": "uid-1",
        "has_entitlement": True,
        "signed_in_at": "2026-07-19T00:00:00Z",
    }
    data.update(overrides)
    kiln_dir = auth_home / ".kiln"
    kiln_dir.mkdir(exist_ok=True)
    (kiln_dir / "auth_tokens.json").write_text(json.dumps(data))
    return data


def _no_network(monkeypatch):
    def _explode(_rt):  # pragma: no cover — the assertion IS not reaching here
        raise AssertionError("network touched on a no-network path")

    monkeypatch.setattr(auth_session, "_post_refresh", _explode)


class TestFastPaths:
    def test_signed_out_when_no_file(self, auth_home, monkeypatch):
        _no_network(monkeypatch)
        result = resolve_session_bearer()
        assert result.state == "signed_out"
        assert result.token == ""
        # ``detail`` surfaces verbatim as license_status's action_required and
        # as a refused hosted call's error, so it is read by a person: it says
        # what is true, and carries no command syntax.  The command travels in
        # the agent-addressed field those responses attach alongside it.
        assert "signed in" in result.detail
        assert "`" not in result.detail, "no command syntax in user-facing copy"

    def test_signed_out_when_access_token_empty(self, auth_home, monkeypatch):
        _no_network(monkeypatch)
        _write_session(auth_home, access_token="")
        assert resolve_session_bearer().state == "signed_out"

    def test_live_token_returned_without_network(self, auth_home, monkeypatch):
        _no_network(monkeypatch)
        data = _write_session(auth_home)
        result = resolve_session_bearer()
        assert result == SessionBearer(token=data["access_token"], state="live")

    def test_no_refresh_token_degrades_with_stored_bearer(
        self, auth_home, monkeypatch
    ):
        _no_network(monkeypatch)
        data = _write_session(
            auth_home, access_token=_jwt(time.time() - 10), refresh_token=""
        )
        result = resolve_session_bearer()
        assert result.state == "degraded"
        assert result.token == data["access_token"]


class TestRefreshExchange:
    def test_expired_token_refreshes_and_persists_rotation(
        self, auth_home, monkeypatch
    ):
        _write_session(auth_home, access_token=_jwt(time.time() - 10))
        new_access = _jwt(time.time() + 3600)
        seen = {}

        def fake_post(rt):
            seen["refresh_token"] = rt
            return 200, {"access_token": new_access, "refresh_token": "rt-rotated"}

        monkeypatch.setattr(auth_session, "_post_refresh", fake_post)

        result = resolve_session_bearer()
        assert result.state == "refreshed"
        assert result.token == new_access
        assert seen["refresh_token"] == "rt-original"

        on_disk = json.loads(
            (auth_home / ".kiln" / "auth_tokens.json").read_text()
        )
        assert on_disk["access_token"] == new_access
        assert on_disk["refresh_token"] == "rt-rotated"
        assert "refreshed_at" in on_disk
        # Sibling fields survive the rewrite untouched.
        assert on_disk["email"] == "user@example.com"
        assert on_disk["tier"] == "enterprise"
        assert on_disk["has_entitlement"] is True

    def test_rewritten_file_keeps_0600(self, auth_home, monkeypatch):
        _write_session(auth_home, access_token=_jwt(time.time() - 10))
        monkeypatch.setattr(
            auth_session,
            "_post_refresh",
            lambda rt: (
                200,
                {"access_token": _jwt(time.time() + 3600), "refresh_token": "r2"},
            ),
        )
        resolve_session_bearer()
        mode = (auth_home / ".kiln" / "auth_tokens.json").stat().st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_near_expiry_token_refreshes_inside_margin(
        self, auth_home, monkeypatch
    ):
        # Valid for 2 more minutes — inside the 5-minute margin.
        _write_session(auth_home, access_token=_jwt(time.time() + 120))
        monkeypatch.setattr(
            auth_session,
            "_post_refresh",
            lambda rt: (
                200,
                {"access_token": _jwt(time.time() + 3600), "refresh_token": "r2"},
            ),
        )
        assert resolve_session_bearer().state == "refreshed"

    def test_malformed_jwt_routes_to_refresh(self, auth_home, monkeypatch):
        _write_session(auth_home, access_token="not-a-jwt")
        monkeypatch.setattr(
            auth_session,
            "_post_refresh",
            lambda rt: (
                200,
                {"access_token": _jwt(time.time() + 3600), "refresh_token": "r2"},
            ),
        )
        assert resolve_session_bearer().state == "refreshed"


class TestRejectedRefresh:
    def test_401_needs_signin_and_verdict_persisted(self, auth_home, monkeypatch):
        _write_session(auth_home, access_token=_jwt(time.time() - 10))
        monkeypatch.setattr(auth_session, "_post_refresh", lambda rt: (401, {}))

        result = resolve_session_bearer()
        assert result.state == "needs_signin"
        assert result.token == ""
        assert "user@example.com" in result.detail
        assert "expired" in result.detail
        # Person-facing copy: no command syntax here (see the signed_out case).
        assert "`" not in result.detail, "no command syntax in user-facing copy"
        # Never destructive of CONTEXT: the file (email/tier) stays for the
        # sign-in hint — but the rejection is now a persisted verdict: the
        # dead refresh token is dropped and the file stamped, so no later
        # caller re-pays the doomed exchange.
        stored = json.loads(
            (auth_home / ".kiln" / "auth_tokens.json").read_text()
        )
        assert stored.get("email") == "user@example.com"
        assert "refresh_token" not in stored
        assert stored.get("refresh_rejected_at")

    def test_rejection_is_terminal_no_network_on_later_resolves(
        self, auth_home, monkeypatch
    ):
        """The 2026-08-20 production loop: the bridge daemon resolved the
        bearer on every reconnect, and every resolve re-POSTed the same
        server-rejected refresh token — 401s every few seconds,
        indefinitely.  After one rejection, later resolves must answer
        from the persisted verdict with ZERO network."""
        _write_session(auth_home, access_token=_jwt(time.time() - 10))
        calls = []

        def rejecting_post(rt):
            calls.append(rt)
            return 401, {}

        monkeypatch.setattr(auth_session, "_post_refresh", rejecting_post)
        assert resolve_session_bearer().state == "needs_signin"
        assert len(calls) == 1

        # Every subsequent resolve: same verdict, no network at all.
        _no_network(monkeypatch)
        for _ in range(3):
            again = resolve_session_bearer()
            assert again.state == "needs_signin"
            assert "user@example.com" in again.detail

    def test_fresh_signin_clears_the_rejection_verdict(
        self, auth_home, monkeypatch
    ):
        _write_session(auth_home, access_token=_jwt(time.time() - 10))
        monkeypatch.setattr(auth_session, "_post_refresh", lambda rt: (401, {}))
        assert resolve_session_bearer().state == "needs_signin"

        # ``kiln signin`` / ``kiln pair`` write a brand-new token file —
        # no stamp survives, and resolution is healthy again.
        _write_session(auth_home)
        _no_network(monkeypatch)
        healthy = resolve_session_bearer()
        assert healthy.state == "live"
        assert healthy.token

    def test_convenience_getter_returns_empty_string(
        self, auth_home, monkeypatch
    ):
        _write_session(auth_home, access_token=_jwt(time.time() - 10))
        monkeypatch.setattr(auth_session, "_post_refresh", lambda rt: (401, {}))
        assert get_paired_access_token() == ""


class TestNetworkDegradation:
    def test_unreachable_endpoint_degrades_with_stored_token(
        self, auth_home, monkeypatch
    ):
        data = _write_session(auth_home, access_token=_jwt(time.time() - 10))
        monkeypatch.setattr(auth_session, "_post_refresh", lambda rt: (0, {}))
        result = resolve_session_bearer()
        assert result.state == "degraded"
        assert result.token == data["access_token"]

    def test_backoff_skips_reattempt_within_interval(self, auth_home, monkeypatch):
        _write_session(auth_home, access_token=_jwt(time.time() - 10))
        calls = []

        def failing_post(rt):
            calls.append(rt)
            return 0, {}

        monkeypatch.setattr(auth_session, "_post_refresh", failing_post)
        resolve_session_bearer()
        resolve_session_bearer()
        assert len(calls) == 1  # second call rode the backoff

    def test_5xx_treated_as_degraded_not_signin(self, auth_home, monkeypatch):
        _write_session(auth_home, access_token=_jwt(time.time() - 10))
        monkeypatch.setattr(auth_session, "_post_refresh", lambda rt: (503, {}))
        assert resolve_session_bearer().state == "degraded"

    def test_malformed_200_body_treated_as_degraded(self, auth_home, monkeypatch):
        _write_session(auth_home, access_token=_jwt(time.time() - 10))
        monkeypatch.setattr(
            auth_session, "_post_refresh", lambda rt: (200, {"access_token": ""})
        )
        assert resolve_session_bearer().state == "degraded"


class TestRefreshLockIsExclusive:
    """The lock itself — that two holders cannot overlap.

    Distinct from the re-read behaviour below: this proves mutual
    exclusion, which matters because Supabase invalidates a refresh
    token on use, so two concurrent exchanges would kill each other.
    """

    def test_second_acquirer_blocks_while_lock_is_held(self, auth_home):
        import fcntl

        lock_path = auth_session._tokens_path().with_suffix(".lock")
        with auth_session._refresh_lock():
            # A separate fd is what another process would get; a
            # non-blocking exclusive take must fail while we hold it.
            lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                with pytest.raises(OSError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    def test_lock_released_after_exit(self, auth_home):
        import fcntl

        lock_path = auth_session._tokens_path().with_suffix(".lock")
        with auth_session._refresh_lock():
            pass
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class TestPostLockRecheck:
    def test_rival_refresh_short_circuits_under_lock(self, auth_home, monkeypatch):
        """If another process rotates the pair while we wait on the lock,
        the post-lock re-read must return its fresh token — no exchange."""
        _write_session(auth_home, access_token=_jwt(time.time() - 10))
        fresh = _jwt(time.time() + 3600)

        import contextlib as _ctx

        @_ctx.contextmanager
        def lock_then_swap():
            _write_session(
                auth_home, access_token=fresh, refresh_token="rt-rival"
            )
            yield

        monkeypatch.setattr(auth_session, "_refresh_lock", lock_then_swap)
        _no_network(monkeypatch)

        result = resolve_session_bearer()
        assert result == SessionBearer(token=fresh, state="live")


class TestNeverRaisesContract:
    """The module promises a state, never an exception — even broken."""

    def test_missing_requests_degrades_instead_of_raising(
        self, auth_home, monkeypatch
    ):
        import sys

        data = _write_session(auth_home, access_token=_jwt(time.time() - 10))
        monkeypatch.setitem(sys.modules, "requests", None)  # → ImportError
        result = resolve_session_bearer()
        assert result.state == "degraded"
        assert result.token == data["access_token"]


class TestServerFallback:
    """A broken resolver must not tell a signed-in user they never paired."""

    def test_pro_api_call_falls_back_to_raw_token(self, auth_home, monkeypatch):
        import sys

        from kiln import server

        data = _write_session(auth_home)
        monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
        monkeypatch.setitem(sys.modules, "kiln.auth_session", None)  # ImportError

        import urllib.request

        sent = {}

        def fake_urlopen(req, timeout=None):
            sent["auth"] = req.get_header("Authorization")
            raise RuntimeError("stop here — the bearer is what we assert")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = server._pro_api_call("some_pro_tool")

        # The stale-but-real bearer went out; no false "not paired".
        assert result.get("code") != "KILN_ACCOUNT_NOT_PAIRED"
        assert sent.get("auth") == f"Bearer {data['access_token']}"

    def test_raw_helper_reads_stored_token(self, auth_home):
        from kiln import server

        data = _write_session(auth_home)
        assert server._raw_paired_access_token() == data["access_token"]
