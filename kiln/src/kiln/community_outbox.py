"""Durable outbox for community-intelligence contributions.

Community contribution is invisible to the user — opt in once, then total
silence.  Because no human ever sees it, it must never *silently* drop: a
failed send can't rely on someone noticing.  So every contribution is
persisted locally first, then sent; a failed send stays queued and is
retried on the next contribution, on startup, and periodically.  Sends are
idempotent via a per-contribution dedupe key.

The outbox keeps its own SQLite file (``community_outbox.db``, co-located
with ``kiln.db``), opened WAL + busy_timeout and routed through
``persistence._retry_on_locked`` — the same write-lock hardening kiln.db
uses — so a contended local write is never lost either.

Maintainer-only observability lives in :func:`status`; it is never surfaced
to the user (per the design: opt in once, then silence).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any

from kiln.persistence import _retry_on_locked

logger = logging.getLogger(__name__)

# Stop retrying a contribution after this many failed sends.  At that point
# it is "stuck" — surfaced to maintainers via status(), never to the user —
# rather than retried forever (a permanently-bad row shouldn't block the queue).
_MAX_ATTEMPTS = 8
_DRAIN_BATCH = 50

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def _db_path() -> str:
    """Co-locate the outbox with kiln.db, honoring the KILN_DB_PATH override
    (so tests can point it at a tmp dir)."""
    base = os.environ.get("KILN_DB_PATH") or os.path.join(
        os.path.expanduser("~"), ".kiln", "kiln.db"
    )
    return os.path.join(os.path.dirname(base) or ".", "community_outbox.db")


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = _db_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS community_outbox (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key  TEXT UNIQUE NOT NULL,
                payload     TEXT NOT NULL,
                created_at  REAL NOT NULL,
                sent_at     REAL,
                attempts    INTEGER NOT NULL DEFAULT 0,
                last_error  TEXT
            )
            """
        )
        conn.commit()
        _conn = conn
    return _conn


def close() -> None:
    """Close the cached connection.  Used at shutdown and by tests to reset
    between temp databases."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def enqueue(dedupe_key: str, record: dict[str, Any]) -> bool:
    """Persist a contribution durably.  Idempotent — a duplicate dedupe_key
    is ignored.  Returns True if newly queued, False if already present."""
    with _lock:
        conn = _db()
        cur = _retry_on_locked(
            lambda: conn.execute(
                "INSERT OR IGNORE INTO community_outbox "
                "(dedupe_key, payload, created_at) VALUES (?, ?, ?)",
                (dedupe_key, json.dumps(record, sort_keys=True), time.time()),
            )
        )
        _retry_on_locked(conn.commit)
        return cur.rowcount > 0


def drain(batch: int = _DRAIN_BATCH) -> dict[str, int]:
    """Send queued contributions.  Best-effort: a failed send stays queued
    (attempts incremented) for the next drain.  Never raises.  Returns
    ``{sent, failed, remaining}``."""
    from kiln.community_sync import sync_community_print  # lazy: avoid cycle

    sent = failed = 0
    with _lock:
        conn = _db()
        rows = _retry_on_locked(
            lambda: conn.execute(
                "SELECT id, payload FROM community_outbox "
                "WHERE sent_at IS NULL AND attempts < ? ORDER BY id LIMIT ?",
                (_MAX_ATTEMPTS, batch),
            )
        ).fetchall()
        for row in rows:
            ok = False
            try:
                ok = bool(sync_community_print(json.loads(row["payload"])))
            except Exception as exc:  # noqa: BLE001 — never let one bad row break the drain
                logger.debug("community outbox send error id=%s: %s", row["id"], exc)
            if ok:
                _retry_on_locked(
                    lambda r=row: conn.execute(
                        "UPDATE community_outbox SET sent_at = ? WHERE id = ?",
                        (time.time(), r["id"]),
                    )
                )
                sent += 1
            else:
                _retry_on_locked(
                    lambda r=row: conn.execute(
                        "UPDATE community_outbox SET attempts = attempts + 1, "
                        "last_error = ? WHERE id = ?",
                        ("send failed", r["id"]),
                    )
                )
                failed += 1
        _retry_on_locked(conn.commit)
        remaining = _retry_on_locked(
            lambda: conn.execute(
                "SELECT COUNT(*) AS n FROM community_outbox "
                "WHERE sent_at IS NULL AND attempts < ?",
                (_MAX_ATTEMPTS,),
            )
        ).fetchone()["n"]
    return {"sent": sent, "failed": failed, "remaining": remaining}


def _safe_drain() -> None:
    try:
        drain()
    except Exception as exc:  # noqa: BLE001 — background flush must never crash
        logger.debug("community outbox drain error: %s", exc)


def contribute(dedupe_key: str, record: dict[str, Any]) -> dict[str, Any]:
    """Queue a contribution durably and flush it in the background.

    Silent and non-blocking; never raises.  The enqueue is synchronous (the
    durability guarantee — the row is on disk before we return); the send
    runs on a daemon thread.  If that send fails (offline, crash), the row
    persists and is retried by the next drain.  Gated on community opt-in —
    opted-out users enqueue nothing.
    """
    try:
        from kiln.community_sync import community_opt_in_enabled  # lazy: avoid cycle

        if not community_opt_in_enabled():
            return {"opted_out": True, "queued": False}
        newly = enqueue(dedupe_key, record)
        threading.Thread(
            target=_safe_drain, daemon=True, name="kiln-community-outbox-drain"
        ).start()
        return {"queued": newly}
    except Exception as exc:  # noqa: BLE001 — contribution must never break the caller
        logger.debug("community contribute failed (non-fatal): %s", exc)
        return {"queued": False, "error": str(exc)}


def status() -> dict[str, int]:
    """Maintainer-only health view: contribution counts by state.  Never
    user-facing.  ``stuck`` > 0 means sends are failing past _MAX_ATTEMPTS —
    investigate the federation endpoint, not the user."""
    with _lock:
        conn = _db()

        def c(sql: str, params: tuple = ()) -> int:
            return _retry_on_locked(lambda: conn.execute(sql, params)).fetchone()["c"]

        return {
            "pending": c(
                "SELECT COUNT(*) AS c FROM community_outbox "
                "WHERE sent_at IS NULL AND attempts < ?",
                (_MAX_ATTEMPTS,),
            ),
            "sent": c("SELECT COUNT(*) AS c FROM community_outbox WHERE sent_at IS NOT NULL"),
            "stuck": c(
                "SELECT COUNT(*) AS c FROM community_outbox "
                "WHERE sent_at IS NULL AND attempts >= ?",
                (_MAX_ATTEMPTS,),
            ),
            "total": c("SELECT COUNT(*) AS c FROM community_outbox"),
        }
