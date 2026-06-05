"""On-device usage ledger — count this install's local tool calls.

Every MCP tool dispatch is tallied into a small SQLite ledger under
``~/.kiln`` that never leaves the machine.  :func:`record` is wired from
``server._record_local_tool_call`` so it sits on the hot path of every
tool call and is therefore strictly best-effort: it swallows its own
exceptions and returns fast.  A metrics gap is fine; a broken tool call
is not.

The cloud flush (:func:`flush`) batches these aggregates up to
``POST /api/me/stats/record`` when the user is signed in
(``python3 -m kiln signin``) and online, so local agent work counts on
their ``/stats`` dashboard — not just web-app activity.  Counting is
idempotent across retries and machines: we send each ``(day, tool)``
row's ABSOLUTE local count plus a stable ``device_id``; the server keys
one row per ``(tenant, device, day, tool)`` and the dashboard SUMs
across devices, so re-sending the same count is a no-op.  That is why a
crash between "server wrote" and "local marked synced" can never
double-count.

This is the public-Kiln (free-tier) recorder.  It is a standalone port
of kiln-pro's ``kiln_pro/usage/local_recorder.py`` with two swaps: the
OAuth bearer comes from ``server._paired_access_token`` and the API base
from ``server._HOSTED_KILN_API_URL`` (both lazily imported so this stays
a light leaf module).  Exactly one recorder fires per machine — when
kiln-pro is installed the hook uses its recorder instead, so the two
never double-count (see ``server._record_local_tool_call``).
"""
from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Matches the CHECK on the cloud tables — a too-long name is dropped
# here rather than rejected on flush.
_MAX_TOOL_LEN = 120


def _kiln_dir() -> Path:
    """``~/.kiln`` (override with ``KILN_HOME``), created on demand."""
    d = Path(os.environ.get("KILN_HOME", "").strip() or (Path.home() / ".kiln"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ledger_path() -> Path:
    """Where the SQLite ledger lives (override with ``KILN_USAGE_LEDGER_PATH``)."""
    override = os.environ.get("KILN_USAGE_LEDGER_PATH", "").strip()
    if override:
        p = Path(override)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    d = _kiln_dir() / "usage"
    d.mkdir(parents=True, exist_ok=True)
    return d / "ledger.sqlite"


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _connect() -> sqlite3.Connection | None:
    """Open the ledger, creating the schema on first use.  None on failure."""
    try:
        conn = sqlite3.connect(str(_ledger_path()), timeout=2.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS daily_tool_calls ("
            " day TEXT NOT NULL,"
            " tool TEXT NOT NULL,"
            " n INTEGER NOT NULL DEFAULT 0,"
            " synced_n INTEGER NOT NULL DEFAULT 0,"
            " PRIMARY KEY (day, tool))"
        )
        return conn
    except Exception:
        logger.debug("usage: ledger open failed", exc_info=True)
        return None


def record(tool_name: str) -> None:
    """Best-effort: bump today's local counter for *tool_name* by one.

    Hot path — never raises, returns fast.  Empty or over-length names
    are dropped silently (a metrics gap is acceptable; a raised
    exception on a tool dispatch is not).
    """
    if not tool_name or len(tool_name) > _MAX_TOOL_LEN:
        return
    conn = _connect()
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                "INSERT INTO daily_tool_calls (day, tool, n) VALUES (?, ?, 1)"
                " ON CONFLICT(day, tool) DO UPDATE SET n = n + 1",
                (_today(), tool_name),
            )
    except Exception:
        logger.debug("usage: record failed for %s", tool_name, exc_info=True)
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def device_id() -> str:
    """Stable per-machine id at ``~/.kiln/device_id`` (honors ``KILN_HOME``).

    Read-or-create.  This is the same path kiln-pro's recorder uses, so a
    machine keeps ONE identity if it later adds kiln-pro — the server
    dedups this device's rows against itself and nothing double-counts
    across the free→Pro boundary.  Returns a sentinel rather than raising
    if the file is unreadable; the flush still works, it just can't dedup
    that machine's rows against itself across reinstalls.
    """
    path = _kiln_dir() / "device_id"
    try:
        existing = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        if existing:
            return existing
        new_id = uuid.uuid4().hex
        path.write_text(new_id, encoding="utf-8")
        return new_id
    except Exception:
        logger.debug("usage: device_id resolve failed", exc_info=True)
        return "unknown-device"


# --- Layer 2: best-effort cloud flush -----------------------------------

_FLUSH_INTERVAL_S = 300.0  # at most one network flush per 5 minutes
_MAX_FLUSH_ENTRIES = 1000

_last_flush = 0.0
_flush_lock = threading.Lock()


def _api_base() -> str:
    """The hosted API base.  ``KILN_API_URL`` override else the default.

    Mirrors ``server._pro_api_call``'s resolution and reuses
    ``server._HOSTED_KILN_API_URL`` as the default (lazily imported to
    keep this module independent of the heavy server module).  The
    env override short-circuits before the import, which is also what
    keeps the flush tests from pulling in ``kiln.server``.
    """
    override = (os.environ.get("KILN_API_URL") or "").strip()
    if override:
        return override.rstrip("/")
    try:
        from kiln.server import _HOSTED_KILN_API_URL

        return _HOSTED_KILN_API_URL.rstrip("/")
    except Exception:
        # Defensive only — the hook runs inside server, so the import
        # above effectively always succeeds in production.
        return "https://api.kiln3d.com"


def _is_safe_base(base: str) -> bool:
    """True if it's safe to put the OAuth bearer on the wire for *base*.

    Require https so the token is never sent in cleartext; allow
    localhost over http for tests / local dev.
    """
    return (
        base.startswith("https://")
        or base.startswith("http://127.0.0.1")
        or base.startswith("http://localhost")
    )


def _oauth_token() -> str | None:
    """The signed-in user's OAuth access token, or None if not signed in.

    Reuses public Kiln's own resolver (``server._paired_access_token``),
    which reads ``~/.kiln/auth_tokens.json`` (honoring ``KILN_AUTH_HOME``)
    — the same file ``python3 -m kiln signin`` writes.  A license-key
    bearer is intentionally NOT used here: the per-user ``/stats`` table
    is JWT-only, so a license bearer would just be recorded as 0.
    """
    try:
        from kiln.server import _paired_access_token

        return _paired_access_token() or None
    except Exception:
        logger.debug("usage: oauth token resolve failed", exc_info=True)
        return None


def flush() -> int:
    """Push not-yet-synced local counts to the hosted endpoint.

    Best-effort and idempotent: each changed row's ABSOLUTE count is
    sent; the server stores ``GREATEST(existing, count)`` keyed on this
    machine's ``device_id``, so a retry is a no-op.  ``synced_n`` is
    advanced only on a CONFIRMED write, so nothing is lost if the network
    drops mid-flush.  Returns the number of rows synced; 0 (and never
    raises) when not signed in, offline, or nothing changed.
    """
    token = _oauth_token()
    if not token:
        return 0
    base = _api_base()
    if not _is_safe_base(base):
        logger.debug("usage: refusing to send bearer to non-https base %s", base)
        return 0
    conn = _connect()
    if conn is None:
        return 0
    try:
        rows = conn.execute(
            "SELECT day, tool, n FROM daily_tool_calls"
            " WHERE n > synced_n ORDER BY day LIMIT ?",
            (_MAX_FLUSH_ENTRIES,),
        ).fetchall()
        if not rows:
            return 0
        entries = [{"day": d, "tool": t, "count": n} for d, t, n in rows]

        import httpx

        resp = httpx.post(
            f"{base}/api/me/stats/record",
            headers={"Authorization": f"Bearer {token}"},
            json={"device_id": device_id(), "entries": entries},
            timeout=10.0,
        )
        if resp.status_code != 200 or not resp.json().get("success"):
            logger.debug("usage: flush not confirmed (HTTP %s)", resp.status_code)
            return 0
        # Confirmed — mark each row synced at the count we actually sent.
        # The ``synced_n < ?`` guard keeps a concurrent flush from
        # lowering an already-higher watermark.
        with conn:
            for d, t, n in rows:
                conn.execute(
                    "UPDATE daily_tool_calls SET synced_n = ?"
                    " WHERE day = ? AND tool = ? AND synced_n < ?",
                    (n, d, t, n),
                )
        return len(rows)
    except Exception:
        logger.debug("usage: flush failed", exc_info=True)
        return 0
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _flush_safe() -> None:
    try:
        flush()
    except Exception:
        logger.debug("usage: flush thread crashed", exc_info=True)


def maybe_flush() -> None:
    """Trigger a background flush at most once per ``_FLUSH_INTERVAL_S``.

    Called from the hot path (every tool call) via the hook, so it stays
    cheap: it only checks a timestamp and, when due, hands the network
    flush to a daemon thread.  Never blocks, never raises.
    """
    global _last_flush
    now = time.monotonic()
    with _flush_lock:
        if now - _last_flush < _FLUSH_INTERVAL_S:
            return
        _last_flush = now
    try:
        threading.Thread(
            target=_flush_safe, name="kiln-usage-flush", daemon=True
        ).start()
    except Exception:
        logger.debug("usage: could not start flush thread", exc_info=True)
