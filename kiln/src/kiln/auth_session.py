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
                      Persisted, and honoured before the clock: a revoked
                      session's access token can read valid for an hour.
    ``signed_out``    no session file / no access token at all.

A clock-valid token the server keeps refusing is the one case the fast
path cannot judge; ``verify=True`` settles it with a single exchange
(see :func:`resolve_session_bearer`).

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
    """The person-facing half of an expired session.

    ``detail`` is not an internal field: it surfaces verbatim as
    ``license_status``'s ``action_required`` and as the ``error`` of a refused
    hosted call, so it is read by someone who was in the middle of making
    something.  The command belongs in the agent-addressed field those two
    responses carry alongside it, not here.
    """
    from kiln.tiers_and_terms import session_expired_message

    return session_expired_message(str(stored.get("email") or ""))


def _rejected_verdict(stored: dict) -> SessionBearer | None:
    """The persisted ``needs_signin`` verdict, when the file carries one.

    A refresh the server REJECTED is a settled verdict, not a retriable
    condition — the rejection handler strips the dead refresh token and
    stamps the file, and every resolve honours the stamp so no later
    caller re-pays the doomed exchange.  Without it, every long-lived
    caller (the bridge daemon resolves the bearer on every reconnect)
    re-POSTed the same dead token indefinitely: production logs showed
    the loop running for minutes at a time, 401 after 401 (2026-08-20).
    Only a fresh ``kiln signin`` / ``kiln pair`` — which writes a new
    token file with no stamp — clears it.

    Honoured BEFORE the clock is consulted: the access token beside the
    stamp may read valid for up to an hour, and the server refuses it on
    every use regardless (a session terminated server-side keeps a JWT
    that says ``exp`` in the future).  Judged by the clock alone, that
    hour is an hour of "live" answers from a session that is not.
    """
    if stored.get("refresh_rejected_at") and not str(
        stored.get("refresh_token") or ""
    ).strip():
        return SessionBearer(token="", state="needs_signin", detail=_signin_hint(stored))
    return None


def resolve_session_bearer(
    refresh_margin_s: float = DEFAULT_REFRESH_MARGIN_S,
    *,
    verify: bool = False,
) -> SessionBearer:
    """Return a currently-valid session bearer, refreshing if needed.

    Never raises; every outcome is a :class:`SessionBearer` state the
    caller can act on.  See the module docstring for the state table.

    ``verify`` asks the SERVER whether the session is still alive, with
    one refresh exchange, even when the clock calls the token valid.  A
    caller reaches for it after the server has refused a token the clock
    vouches for — the case the clock cannot see: a session revoked
    server-side (signed out elsewhere, or ended by Supabase's session
    limits) keeps a JWT that reads valid until ``exp`` while every request
    it makes is refused.  Measured 2026-09-24: 591 relay refusals over a
    token whose ``exp`` was still forty minutes out, and no sign-in hint,
    because the clock said ``live``.  The exchange either mints a fresh
    pair (``refreshed`` — the refusal was something else; retry with the
    new token), is rejected (``needs_signin``, persisted so every later
    resolve on this machine says so without a network call), or cannot be
    made (``degraded`` — the stored token stays in play).  One exchange
    per call, under the same cross-process lock as a routine refresh; a
    rival that verified first is honoured from its re-read, not repeated.
    """
    stored = _read_tokens()
    token = str(stored.get("access_token") or "").strip()
    if not token:
        from kiln.tiers_and_terms import signed_out_message

        return SessionBearer(
            token="",
            state="signed_out",
            detail=signed_out_message(),
        )

    if (rejected := _rejected_verdict(stored)) is not None:
        return rejected

    if not verify and _seconds_to_expiry(token) > refresh_margin_s:
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
        # Another process may have acted while we waited on the lock —
        # re-read, and honour what it found before exchanging anything.
        stored = _read_tokens()
        if (rejected := _rejected_verdict(stored)) is not None:
            # A rival's exchange was refused: the verdict is on file, and
            # POSTing our copy of the same dead token again tells the
            # server nothing it has not already said.
            return rejected
        current = str(stored.get("access_token") or "").strip()
        if verify:
            if current and current != token:
                # A rival verified (or refreshed) first and the file
                # carries its answer; the token we were asked about is
                # gone, and the one on file is the server's newer word.
                return SessionBearer(token=current, state="refreshed")
        elif current and _seconds_to_expiry(current) > refresh_margin_s:
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
            # revoked, or expired server-side.  Not recoverable here —
            # and not recoverable NEXT time either, so persist the
            # verdict: drop the dead refresh token and stamp the file.
            # Subsequent resolves short-circuit to ``needs_signin`` with
            # no network call (see the stamp check above); a new
            # sign-in writes a fresh file and everything recovers.
            # Email and the rest of the record stay, so the sign-in
            # hint keeps its context — including the (near-expiry)
            # access token, whose presence is what routes later
            # resolves through the stamp check rather than the plainer
            # ``signed_out`` branch.
            merged = dict(stored)
            merged.pop("refresh_token", None)
            merged["refresh_rejected_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            try:
                _write_tokens(merged)
            except OSError:
                # Unpersistable verdict: the loop this exists to stop
                # survives on this machine.  Say so once, loudly.
                logger.warning(
                    "auth_session: refresh was rejected but %s could not "
                    "be updated — callers will keep re-attempting the "
                    "exchange until `kiln signin` rewrites it.",
                    _tokens_path(),
                )
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


@dataclass(frozen=True)
class ApiBearer:
    """Which credential this machine should present to the Kiln API.

    ``state`` is ``"license"`` for an operator-supplied license key, or the
    :class:`SessionBearer` state for a paired sign-in session.  ``token``
    is empty only for ``needs_signin`` / ``signed_out``, and ``detail``
    then carries the one sentence that tells the user how to fix it.
    """

    token: str
    state: str
    detail: str = ""


def resolve_api_bearer(
    refresh_margin_s: float = DEFAULT_REFRESH_MARGIN_S,
    *,
    verify: bool = False,
) -> ApiBearer:
    """The bearer for ANY authenticated call to the Kiln API.

    One resolver for every caller — hosted tool calls, community reads,
    anything that follows.  Resolution order:

      1. ``KILN_LICENSE_KEY`` — an operator-supplied license wins, and
         needs no refresh.
      2. The paired sign-in session (``kiln signin`` / ``kiln pair``),
         transparently refreshed near expiry by
         :func:`resolve_session_bearer`.
      3. Nothing — ``token`` is empty and ``state`` says which kind of
         nothing, so a caller can tell "never signed in" from "session
         expired" and say something useful either way.

    ``verify`` is :func:`resolve_session_bearer`'s: one exchange with the
    server on a session the clock still vouches for.  A license key needs
    no such check and never pays for one.

    Never raises.
    """
    license_key = os.environ.get("KILN_LICENSE_KEY", "").strip()
    if license_key:
        return ApiBearer(token=license_key, state="license")

    session = resolve_session_bearer(refresh_margin_s, verify=verify)
    return ApiBearer(
        token=session.token, state=session.state, detail=session.detail
    )
