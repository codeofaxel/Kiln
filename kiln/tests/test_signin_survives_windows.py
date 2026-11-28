"""A sign-in survives a second Kiln process and a busy file on Windows.

Every Kiln process on a computer reads one sign-in file.  Windows refuses to
replace a file another process has open, waits a bounded time for a lock
another process holds, and the server ends a session whose refresh token is
used twice.  Each test stands in for one of those with the smallest fake that
shows the behaviour on any platform:

* a replace refused while the file is busy is tried again, and a write that
  never lands leaves no temp file behind;
* a renewal still running in another process after the whole wait is left to
  finish: this process keeps the stored token and does not use the refresh
  token a second time;
* the account's plan is saved onto the file as it is under the renewal lock,
  so a pair renewed meanwhile is kept, and the plan is still answered when the
  save cannot be made.
"""

from __future__ import annotations

import base64
import contextlib
import json
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

import kiln.auth_session as auth_session
import kiln.file_lock as file_lock
import kiln.plan_proof as plan_proof
from kiln import account_plan
from kiln.account_plan import LicenseTier
from kiln.auth_session import SessionBearer, resolve_session_bearer

_PAID_PLAN_ANSWER = {"success": True, "tier": "pro", "has_entitlement": True}


def _jwt(exp: float) -> str:
    """Minimal unsigned JWT with the given ``exp`` claim."""
    seg = lambda d: base64.urlsafe_b64encode(  # noqa: E731
        json.dumps(d).encode()
    ).rstrip(b"=").decode()
    return f"{seg({'alg': 'none'})}.{seg({'exp': exp})}.sig"


@pytest.fixture()
def auth_home(tmp_path, monkeypatch):
    """A private sign-in file, no network, and no memory of earlier asks."""
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    monkeypatch.setattr(auth_session, "_last_network_failure_monotonic", None)
    monkeypatch.setattr(auth_session, "_post_json", lambda *a, **k: (0, {}))
    monkeypatch.setattr(account_plan, "_last_ask_at", 0.0)
    monkeypatch.setattr(account_plan, "_last_ask_answered", True)
    # A paid plan the answer states counts whatever today's date is.
    monkeypatch.setattr(plan_proof, "UNPROVEN_PLAN_HONOURED_UNTIL", float("inf"))
    return tmp_path


def _tokens_file(home: Path) -> Path:
    return home / ".kiln" / "auth_tokens.json"


def _write_session(home: Path, **overrides) -> dict:
    data = {
        "access_token": _jwt(time.time() + 3600),
        "refresh_token": "rt-original",
        "email": "user@example.com",
        "tier": "free",
        "signed_in_at": int(time.time()),
    }
    data.update(overrides)
    _tokens_file(home).parent.mkdir(exist_ok=True)
    _tokens_file(home).write_text(json.dumps(data))
    return data


def _stored(home: Path) -> dict:
    return json.loads(_tokens_file(home).read_text())


def _temp_files(home: Path) -> list[Path]:
    return list(_tokens_file(home).parent.glob("auth_tokens.tmp*"))


@pytest.fixture()
def short_wait(monkeypatch):
    """The Windows lock wait, cut short so a test can sleep past it."""
    monkeypatch.setattr(file_lock, "DEFAULT_WINDOWS_WAIT_S", 0.05)


def _lock_not_taken(monkeypatch, *, after_the_whole_wait: bool) -> None:
    """The lock as Windows reports it when another process holds it past the
    whole wait, or as a filesystem with no lock support reports it at once."""

    @contextlib.contextmanager
    def not_taken(fd, *, timeout=None):
        if after_the_whole_wait:
            time.sleep(timeout + 0.05)
        yield False

    monkeypatch.setattr(file_lock, "exclusive_file_lock", not_taken)


def _exchanges(monkeypatch) -> list[str]:
    """Every refresh token sent to the server; each one is renewed."""
    sent: list[str] = []

    def post(refresh_token):
        sent.append(refresh_token)
        return 200, {"access_token": _jwt(time.time() + 3600), "refresh_token": "rt-renewed"}

    monkeypatch.setattr(auth_session, "_post_refresh", post)
    return sent


class TestABusyFileIsWrittenOnceItIsFree:
    def _replace_refused(self, monkeypatch, home: Path, times: int | None) -> list[str]:
        """A replace onto the sign-in file is refused *times* times (every
        time for ``None``), as Windows refuses it while another process has
        the file open."""
        real_replace = Path.replace
        target = _tokens_file(home)
        refused: list[str] = []

        def replace(self, dest):
            if Path(dest) == target and (times is None or len(refused) < times):
                refused.append(str(dest))
                raise PermissionError(13, "The file is open in another program")
            return real_replace(self, dest)

        monkeypatch.setattr(Path, "replace", replace)
        monkeypatch.setattr(auth_session, "_REPLACE_RETRY_S", 0.0, raising=False)
        return refused

    def test_a_replace_refused_twice_lands_on_the_third_try(self, auth_home, monkeypatch):
        _write_session(auth_home)
        refused = self._replace_refused(monkeypatch, auth_home, times=2)

        auth_session._write_tokens({"access_token": "at-new", "refresh_token": "rt-new"})

        assert len(refused) == 2
        assert _stored(auth_home) == {"access_token": "at-new", "refresh_token": "rt-new"}
        assert _temp_files(auth_home) == []

    def test_a_replace_never_allowed_raises_and_leaves_no_temp_file(self, auth_home, monkeypatch):
        before = _write_session(auth_home)
        self._replace_refused(monkeypatch, auth_home, times=None)

        with pytest.raises(OSError):
            auth_session._write_tokens({"access_token": "at-new", "refresh_token": "rt-new"})

        assert _stored(auth_home) == before
        assert _temp_files(auth_home) == []


class TestARenewalRunningElsewhereIsLeftToFinish:
    def test_after_the_whole_wait_the_refresh_token_is_not_used_again(
        self, auth_home, monkeypatch, short_wait
    ):
        expiring = _jwt(time.time() + 60)
        _write_session(auth_home, access_token=expiring)
        before = _tokens_file(auth_home).read_bytes()
        _lock_not_taken(monkeypatch, after_the_whole_wait=True)
        sent = _exchanges(monkeypatch)

        result = resolve_session_bearer()

        assert sent == []
        assert result == SessionBearer(token=expiring, state="degraded")
        assert _tokens_file(auth_home).read_bytes() == before

    def test_a_lock_that_cannot_be_taken_at_once_still_renews(
        self, auth_home, monkeypatch, short_wait
    ):
        _write_session(auth_home, access_token=_jwt(time.time() + 60))
        _lock_not_taken(monkeypatch, after_the_whole_wait=False)
        sent = _exchanges(monkeypatch)

        result = resolve_session_bearer()

        assert sent == ["rt-original"]
        assert result.state == "refreshed"
        assert _stored(auth_home)["refresh_token"] == "rt-renewed"


class TestThePlanIsSavedUnderTheRenewalLock:
    def test_a_pair_renewed_while_this_waited_is_kept(self, auth_home, monkeypatch):
        """Another process renewed the sign-in while it held the lock this
        save waited on: the plan lands on the renewed pair, never the old one."""
        _write_session(auth_home)
        renewed = _jwt(time.time() + 7200)
        monkeypatch.setattr(account_plan, "_ask_account", lambda: dict(_PAID_PLAN_ANSWER))
        real_lock = auth_session._refresh_lock
        real_write = auth_session._write_tokens
        holding: list[bool] = []
        writes_while_holding: list[bool] = []

        @contextlib.contextmanager
        def rival_renewed_before_letting_go():
            with real_lock() as rival_renewing:
                _write_session(auth_home, access_token=renewed, refresh_token="rt-rival")
                holding.append(True)
                try:
                    yield rival_renewing
                finally:
                    holding.pop()

        def write(data):
            writes_while_holding.append(bool(holding))
            real_write(data)

        monkeypatch.setattr(auth_session, "_refresh_lock", rival_renewed_before_letting_go)
        monkeypatch.setattr(auth_session, "_write_tokens", write)

        assert account_plan.refresh_plan() is LicenseTier.PRO

        saved = _stored(auth_home)
        assert (saved["access_token"], saved["refresh_token"]) == (renewed, "rt-rival")
        assert saved["tier"] == "pro"
        assert writes_while_holding == [True]

    def test_a_save_while_a_renewal_runs_elsewhere_is_not_made_and_the_plan_is_answered(
        self, auth_home, monkeypatch, short_wait
    ):
        before = _write_session(auth_home)
        monkeypatch.setattr(account_plan, "_ask_account", lambda: dict(_PAID_PLAN_ANSWER))
        _lock_not_taken(monkeypatch, after_the_whole_wait=True)

        assert account_plan.refresh_plan() is LicenseTier.PRO
        assert _stored(auth_home) == before

        with pytest.raises(OSError):
            auth_session.update_tokens(lambda stored: {**stored, "tier": "pro"})
        assert _stored(auth_home) == before

    def test_renewing_and_saving_the_plan_take_the_lock_in_turn(self, auth_home):
        """The ask for the plan renews an expiring sign-in under the lock,
        lets go, then saves the plan under it again.  Taken while already
        held, the lock would wait on itself for ever."""
        _write_session(auth_home, access_token=_jwt(time.time() + 60))
        resp = mock.MagicMock(status_code=200)
        resp.json.return_value = dict(_PAID_PLAN_ANSWER)
        renewed = (200, {"access_token": _jwt(time.time() + 3600), "refresh_token": "rt-renewed"})
        answers: list[LicenseTier] = []

        with (
            mock.patch("requests.get", return_value=resp),
            mock.patch.object(auth_session, "_post_refresh", return_value=renewed),
        ):
            ask = threading.Thread(target=lambda: answers.append(account_plan.refresh_plan()), daemon=True)
            ask.start()
            ask.join(timeout=10)

        assert not ask.is_alive(), "the plan save waited on a lock this thread already held"
        assert answers == [LicenseTier.PRO]
        saved = _stored(auth_home)
        assert (saved["refresh_token"], saved["tier"]) == ("rt-renewed", "pro")
