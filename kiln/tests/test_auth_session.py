"""Tests for :mod:`kiln.auth_session` — the live paired-session resolver.

Covers every state in the resolver's contract: the no-network fast path,
the refresh exchange (rotation persisted, sibling fields preserved,
0600 perms), the rejected-refresh path (file left intact — never
destructive), the unreachable-endpoint degradation with backoff, and
the double-checked-locking short-circuit when a rival process refreshes
first.  All network I/O is monkeypatched; no test talks to a server.
"""

from __future__ import annotations

import base64
import json
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
        assert "kiln signin" in result.detail

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
    def test_401_needs_signin_and_file_left_intact(self, auth_home, monkeypatch):
        _write_session(auth_home, access_token=_jwt(time.time() - 10))
        monkeypatch.setattr(auth_session, "_post_refresh", lambda rt: (401, {}))

        result = resolve_session_bearer()
        assert result.state == "needs_signin"
        assert result.token == ""
        assert "user@example.com" in result.detail
        assert "kiln signin" in result.detail
        # Never destructive: the file (and its email/tier context) stays.
        assert (auth_home / ".kiln" / "auth_tokens.json").exists()

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


class TestDoubleCheckedLocking:
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
