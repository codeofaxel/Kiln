"""Live paired-session bearer — the one way to read the ``kiln signin`` session.

``kiln signin`` / ``kiln pair`` write ``~/.kiln/auth_tokens.json`` with a
Supabase access token that expires roughly an hour later, plus the
refresh token that can mint a successor.  Every consumer that read the
file raw (tier checks, usage recording, hosted pro-tool calls) went
dark at expiry until the user happened to sign in again — the hosted
API rejects the stale bearer and the caller degrades to free tier with
no hint why.

This module closes that loop.  :func:`resolve_session_bearer` returns a
token that is *currently* valid whenever one can be had: it checks the
JWT ``exp`` locally (no network on the fast path) and, within
``refresh_margin_s`` of expiry, exchanges the refresh token through
``POST /api/auth/refresh`` on the Kiln API — the server-side proxy that
already exists for exactly this — then persists the rotated pair
atomically.  Callers get an explicit state instead of a silent dud:

    ``live``          token valid beyond the margin; nothing touched.
    ``refreshed``     new pair minted and persisted; token is fresh.
    ``degraded``      refresh endpoint unreachable; the stored token is
                      returned as-is (the server is the final judge).
    ``needs_signin``  the refresh token was rejected — the session is
                      revoked or too stale to save.  ``token`` is empty
                      and ``detail`` carries the re-signin instruction.
    ``signed_out``    no session file / no access token at all.

Concurrency: several kiln processes (MCP server, usage recorder, CLI)
may hit the margin at once, and Supabase rotates refresh tokens on use,
so two racing refreshes could invalidate each other.  A file lock next
to the token file serializes the exchange per machine, and the winner's
re-read short-circuits the losers (double-checked locking).  Supabase's
own reuse-grace window covers cross-machine races.

Failure backoff: when the refresh endpoint is unreachable we remember
the failure for ``_REFRESH_RETRY_INTERVAL_S`` and skip re-attempts, so
an offline machine doesn't pay a network timeout on every tool call.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_HOSTED_API_URL = "https://api.kiln3d.com"
_REFRESH_ROUTE = "/api/auth/refresh"
_HTTP_TIMEOUT_S = 8.0

# Refresh when the access token has less than this long to live.  Access
# tokens live ~3600 s; a 300 s margin means one refresh per hour of use
# while never handing out a token that could expire mid-request.
DEFAULT_REFRESH_MARGIN_S = 300.0

# After a *network* failure (endpoint unreachable / 5xx), don't
# re-attempt the exchange for this long — return ``degraded`` fast.
# Process-local: N processes each pay one timeout before backing off,
# which is the cost of not putting shared state on disk for a hint.
_REFRESH_RETRY_INTERVAL_S = 60.0
_last_network_failure_monotonic: float | None = None
# The MCP server calls this from request threads; guard the hint so a
# read never sees a half-written value.
_backoff_lock = threading.Lock()


def _backoff_active() -> bool:
    with _backoff_lock:
        last = _last_network_failure_monotonic
    return (
        last is not None
        and time.monotonic() - last < _REFRESH_RETRY_INTERVAL_S
    )


def _note_network_failure() -> None:
    global _last_network_failure_monotonic
    with _backoff_lock:
        _last_network_failure_monotonic = time.monotonic()


@dataclass(frozen=True)
class SessionBearer:
    """Outcome of a session-bearer resolution.

    ``token`` is empty only for ``signed_out`` / ``needs_signin``;
    ``detail`` is a human-actionable sentence for exactly those states.
    """

    token: str
    state: str  # "live" | "refreshed" | "degraded" | "needs_signin" | "signed_out"
    detail: str = ""


def _tokens_path() -> Path:
    """Session file location; ``KILN_AUTH_HOME`` redirects for tests."""
    home = os.environ.get("KILN_AUTH_HOME") or str(Path.home())
    return Path(home) / ".kiln" / "auth_tokens.json"


def _api_base() -> str:
    return (os.environ.get("KILN_API_URL") or _HOSTED_API_URL).rstrip("/")


def _read_tokens() -> dict:
    try:
        data = json.loads(_tokens_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_tokens(data: dict) -> None:
    """Atomic + 0600, per-process temp name so concurrent writers never
    collide on the intermediate file."""
    path = _tokens_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(path)


def _jwt_exp(token: str) -> float | None:
    """The ``exp`` claim, read without verification.

    Client-side we only *schedule* around expiry; trust stays with the
    server, which verifies the signature on every request.  ``None``
    means the claim can't be read — callers treat that as expired so a
    malformed token routes into the refresh path rather than being sent.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        return float(claims["exp"])
    except (IndexError, KeyError, ValueError, TypeError, binascii.Error):
        return None


def _seconds_to_expiry(token: str, now: float | None = None) -> float:
    exp = _jwt_exp(token)
    if exp is None:
        return 0.0
    return exp - (time.time() if now is None else now)


@contextlib.contextmanager
def _refresh_lock():
    """Serialize the refresh exchange across processes on this machine.

    Advisory ``flock`` on a sibling lockfile.  On platforms/filesystems
    without flock the lock degrades to a no-op — Supabase's refresh
    reuse-grace window still absorbs the rare race.
    """
    lock_path = _tokens_path().with_suffix(".lock")
    try:
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield
        return
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        os.close(fd)


def _post_refresh(refresh_token: str) -> tuple[int, dict]:
    """POST the exchange; ``(0, {})`` when the endpoint is unreachable.

    ``requests`` is a hard dependency, but importing inside the try
    keeps this function's "returns, never raises" contract true even on
    a mangled install — the module's whole promise is that callers get
    a state back, never an exception.
    """
    try:
        import requests
    except ImportError:
        return 0, {}

    try:
        resp = requests.post(
            f"{_api_base()}{_REFRESH_ROUTE}",
            json={"refresh_token": refresh_token},
            timeout=_HTTP_TIMEOUT_S,
        )
    except requests.RequestException:
        return 0, {}
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}


def _signin_hint(stored: dict) -> str:
    email = str(stored.get("email") or "").strip()
    who = f" for {email}" if email else ""
    return (
        f"Your Kiln session{who} has expired and could not be refreshed. "
        "Run `python3 -m kiln signin` to sign in again."
    )


def resolve_session_bearer(
    refresh_margin_s: float = DEFAULT_REFRESH_MARGIN_S,
) -> SessionBearer:
    """Return a currently-valid session bearer, refreshing if needed.

    Never raises; every outcome is a :class:`SessionBearer` state the
    caller can act on.  See the module docstring for the state table.
    """
    stored = _read_tokens()
    token = str(stored.get("access_token") or "").strip()
    if not token:
        return SessionBearer(
            token="",
            state="signed_out",
            detail=(
                "No Kiln session found. Run `python3 -m kiln signin`, or "
                "generate a code at https://app.kiln3d.com/connect and run "
                "`python3 -m kiln pair <code>`."
            ),
        )

    if _seconds_to_expiry(token) > refresh_margin_s:
        return SessionBearer(token=token, state="live")

    refresh_token = str(stored.get("refresh_token") or "").strip()
    if not refresh_token:
        # A session written by a pre-refresh client, or pairing flows
        # that mint no refresh token: nothing to exchange.  Hand the
        # stored token to the server anyway — it is the final judge.
        return SessionBearer(token=token, state="degraded")

    # Recent network failure → don't pay another timeout yet.
    if _backoff_active():
        return SessionBearer(token=token, state="degraded")

    with _refresh_lock():
        # Another process may have refreshed while we waited on the
        # lock — re-read and short-circuit if the file is fresh now.
        stored = _read_tokens()
        current = str(stored.get("access_token") or "").strip()
        if current and _seconds_to_expiry(current) > refresh_margin_s:
            return SessionBearer(token=current, state="live")
        refresh_token = str(stored.get("refresh_token") or "").strip() or refresh_token

        status, body = _post_refresh(refresh_token)

        if status == 200 and body.get("access_token") and body.get("refresh_token"):
            merged = dict(stored)
            merged["access_token"] = str(body["access_token"])
            merged["refresh_token"] = str(body["refresh_token"])
            merged["refreshed_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            try:
                _write_tokens(merged)
            except OSError:
                # Unpersistable rotation is worth a loud line: the NEXT
                # refresh will fail (old token rotated away) and force a
                # re-signin.  The returned token is still good now.
                logger.warning(
                    "auth_session: refreshed session could not be written "
                    "to %s — next refresh will require `kiln signin`.",
                    _tokens_path(),
                )
            return SessionBearer(token=merged["access_token"], state="refreshed")

        if status in (400, 401):
            # The refresh token itself was rejected: rotated away,
            # revoked, or expired server-side.  Not recoverable here.
            return SessionBearer(
                token="", state="needs_signin", detail=_signin_hint(stored)
            )

        # Unreachable / 5xx / rate-limited: keep the stored token in
        # play and back off.  The API's own 401 stays the final word.
        _note_network_failure()
        return SessionBearer(token=token, state="degraded")


def get_paired_access_token(
    refresh_margin_s: float = DEFAULT_REFRESH_MARGIN_S,
) -> str:
    """Bearer string or ``""`` — for call sites that only want the token."""
    return resolve_session_bearer(refresh_margin_s).token
