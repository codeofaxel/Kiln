"""Durable outbox for federated-intelligence contributions.

Community/federation contribution is invisible to the user — opt in once, then
total silence.  Because no human ever sees it, it must never *silently* drop: a
failed send can't rely on someone noticing.  So every contribution is persisted
locally first, then sent; a failed send stays queued and is retried on the next
contribution and on startup.  A per-contribution dedupe key makes the local
enqueue idempotent (no double-queue); the drain commits each row's result as it
lands, so an ill-timed crash replays at most one row to the (currently
dedupe-less) federation endpoint.

Kind-aware: each contribution carries a ``kind`` (``community_print``,
``recovery``, ``nozzle_outcome``, ...).  Senders register per kind via
:func:`register_sender`, and the drain dispatches each row to its kind's
sender — so kiln-pro federation producers reuse this durability without the
outbox importing them (no import cycle, graceful when kiln-pro is absent).  The
built-in ``community_print`` sender (push to Supabase ``community_prints``) is
registered at import.

The outbox keeps its own SQLite file (``community_outbox.db``, co-located with
``kiln.db``), opened WAL + busy_timeout and routed through
``persistence._retry_on_locked`` — the same write-lock hardening kiln.db uses —
so a contended local write is never lost either.

Concurrency: ``_db_lock`` guards the SQLite connection and is held only for
short local operations — never across a network send.  ``_drain_lock`` ensures
a single drain runs at a time (a second caller returns immediately rather than
re-sending rows the first is mid-flight on).  Together they keep ``enqueue``
from ever blocking behind an in-flight drain's network I/O — the property that
lets print-completion handlers call ``contribute`` without risking a stall.

Maintainer-only observability lives in :func:`status` (never surfaced to the
user); additionally, a row that exhausts its retries logs one WARNING, so a
down federation endpoint shows up in the logs rather than only in a count
nobody reads.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from kiln.persistence import _retry_on_locked

logger = logging.getLogger(__name__)

# Stop retrying a contribution after this many failed sends.  At that point it
# is "stuck" — surfaced to maintainers via a one-time WARNING and status(),
# never to the user — rather than retried forever (a permanently-bad row
# shouldn't block the queue behind it).
_MAX_ATTEMPTS = 8
_DRAIN_BATCH = 50

#: The default contribution kind — anonymous community print outcomes.
DEFAULT_KIND = "community_print"

_conn: sqlite3.Connection | None = None
# Guards every access to the SQLite connection.  Held only for short local
# operations — NEVER across a network send (see drain()).
_db_lock = threading.Lock()
# Ensures only one drain runs at a time so two drains can't both claim and send
# the same rows (the federation endpoint has no server-side dedupe).
_drain_lock = threading.Lock()

# Per-kind sender registry.  ``fn(payload, send_id) -> bool``; optional
# ``gate() -> bool`` decides whether opted-in (an opted-out kind enqueues
# nothing).  Registering by kind lets kiln-pro producers reuse this durable
# queue without the outbox importing them — no cycle, graceful if absent.
_SenderEntry = tuple[Callable[[dict[str, Any], str | None], bool], Callable[[], bool] | None]
_senders: dict[str, _SenderEntry] = {}


def register_sender(
    kind: str,
    fn: Callable[[dict[str, Any], str | None], bool],
    *,
    gate: Callable[[], bool] | None = None,
) -> None:
    """Register the sender for a contribution ``kind``.

    :param fn: ``fn(payload, send_id) -> bool`` — perform the network send,
        returning True on success.  ``send_id`` is a random per-contribution
        idempotency token (pass it to the endpoint's upsert if it supports
        one).  Must not raise for control flow; the drain treats exceptions as
        a failed send and retries later.
    :param gate: Optional ``gate() -> bool`` consulted by :func:`contribute`;
        when it returns False the contribution is dropped at enqueue (opted
        out).  Omit for kinds that are always on.
    """
    _senders[kind] = (fn, gate)


def _community_sender(payload: dict[str, Any], send_id: str | None) -> bool:
    # Lazy import avoids a module cycle (community_sync imports nothing from
    # here, but keeping the import lazy matches the rest of the file and lets
    # tests patch kiln.community_sync.sync_community_print).
    from kiln.community_sync import sync_community_print

    return sync_community_print(payload, send_id=send_id)


def _community_gate() -> bool:
    from kiln.community_sync import community_opt_in_enabled

    return community_opt_in_enabled()


register_sender(DEFAULT_KIND, _community_sender, gate=_community_gate)


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
                last_error  TEXT,
                send_id     TEXT,
                kind        TEXT NOT NULL DEFAULT 'community_print'
            )
            """
        )
        # ``send_id`` (random per-contribution idempotency token) and ``kind``
        # were added after the original schema; add them to pre-existing DBs.
        # ``dedupe_key`` is the *local* uniqueness guard (no double-queue);
        # ``send_id`` is the *server* idempotency target (a crash-replayed row
        # re-POSTs the same random id, folded to a no-op by the federation
        # unique index).
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(community_outbox)")}
        if "send_id" not in cols:
            conn.execute("ALTER TABLE community_outbox ADD COLUMN send_id TEXT")
        if "kind" not in cols:
            # Constant DEFAULT backfills existing rows to the community kind.
            conn.execute(
                "ALTER TABLE community_outbox ADD COLUMN kind TEXT NOT NULL "
                "DEFAULT 'community_print'"
            )
        conn.execute(
            "UPDATE community_outbox SET send_id = lower(hex(randomblob(16))) "
            "WHERE send_id IS NULL"
        )
        conn.commit()
        _conn = conn
    return _conn


def close() -> None:
    """Close the cached connection.  Used at shutdown and by tests to reset
    between temp databases."""
    global _conn
    with _db_lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def enqueue(dedupe_key: str, record: dict[str, Any], *, kind: str = DEFAULT_KIND) -> bool:
    """Persist a contribution durably.  Idempotent — a duplicate dedupe_key is
    ignored.  Returns True if newly queued, False if already present.

    Holds ``_db_lock`` only for the local INSERT, so it never blocks behind a
    drain's network I/O."""
    with _db_lock:
        conn = _db()
        cur = _retry_on_locked(
            lambda: conn.execute(
                "INSERT OR IGNORE INTO community_outbox "
                "(dedupe_key, payload, created_at, send_id, kind) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    dedupe_key,
                    json.dumps(record, sort_keys=True),
                    time.time(),
                    uuid.uuid4().hex,
                    kind,
                ),
            )
        )
        _retry_on_locked(conn.commit)
        return cur.rowcount > 0


def _count_pending() -> int:
    """Rows still eligible for a send (unsent, under the attempt cap)."""
    with _db_lock:
        conn = _db()
        return _retry_on_locked(
            lambda: conn.execute(
                "SELECT COUNT(*) AS n FROM community_outbox "
                "WHERE sent_at IS NULL AND attempts < ?",
                (_MAX_ATTEMPTS,),
            )
        ).fetchone()["n"]


def drain(batch: int = _DRAIN_BATCH) -> dict[str, int]:
    """Send queued contributions, dispatching each to its kind's sender.

    Best-effort: a failed send stays queued (attempts incremented) for the next
    drain.  Never raises.  Returns ``{sent, failed, remaining}``.

    Concurrency: the network send runs with NO lock held, so a slow/offline
    send never blocks ``enqueue``.  ``_db_lock`` is taken only to claim the
    batch and to record each row's result (committed per row, so a crash
    replays at most one row).  ``_drain_lock`` admits a single drain at a time;
    a second concurrent caller returns ``sent=0`` immediately.
    """
    if not _drain_lock.acquire(blocking=False):
        # Another drain owns the queue right now — don't double-send.
        return {"sent": 0, "failed": 0, "remaining": _count_pending()}
    try:
        sent = failed = 0
        # 1) Claim a batch under a short lock — no network here.
        with _db_lock:
            conn = _db()
            rows = _retry_on_locked(
                lambda: conn.execute(
                    "SELECT id, dedupe_key, payload, attempts, send_id, kind "
                    "FROM community_outbox "
                    "WHERE sent_at IS NULL AND attempts < ? ORDER BY id LIMIT ?",
                    (_MAX_ATTEMPTS, batch),
                )
            ).fetchall()

        # 2) Send each row with NO lock held; record + commit the result per
        #    row under the lock.  Per-row commit keeps the crash-replay window
        #    to a single row.
        for row in rows:
            ok = False
            entry = _senders.get(row["kind"])
            if entry is None:
                # No sender registered for this kind (e.g. kiln-pro not loaded).
                # Treat as a failed send so it retries / eventually surfaces as
                # stuck, rather than silently looping.
                logger.debug(
                    "community outbox: no sender for kind=%s (id=%s)",
                    row["kind"],
                    row["id"],
                )
            else:
                try:
                    ok = bool(entry[0](json.loads(row["payload"]), row["send_id"]))
                except Exception as exc:  # noqa: BLE001 — one bad row must not break the drain
                    logger.debug("community outbox send error id=%s: %s", row["id"], exc)

            with _db_lock:
                conn = _db()
                if ok:
                    _retry_on_locked(
                        lambda r=row, c=conn: c.execute(
                            "UPDATE community_outbox SET sent_at = ? WHERE id = ?",
                            (time.time(), r["id"]),
                        )
                    )
                    sent += 1
                else:
                    _retry_on_locked(
                        lambda r=row, c=conn: c.execute(
                            "UPDATE community_outbox SET attempts = attempts + 1, "
                            "last_error = ? WHERE id = ?",
                            ("send failed", r["id"]),
                        )
                    )
                    failed += 1
                    if row["attempts"] + 1 >= _MAX_ATTEMPTS:
                        # First crossing into "stuck": surface it once to the
                        # logs.  Maintainer signal (federation endpoint down?),
                        # never the user.  Only one drain runs at a time, so
                        # row["attempts"]+1 is the post-update value.
                        logger.warning(
                            "community outbox row stuck after %d attempts "
                            "(kind=%s, dedupe_key=%s) — sends are failing; check "
                            "the federation endpoint, not the user.",
                            _MAX_ATTEMPTS,
                            row["kind"],
                            row["dedupe_key"],
                        )
                _retry_on_locked(conn.commit)

        return {"sent": sent, "failed": failed, "remaining": _count_pending()}
    finally:
        _drain_lock.release()


def _safe_drain() -> None:
    try:
        drain()
    except Exception as exc:  # noqa: BLE001 — background flush must never crash
        logger.debug("community outbox drain error: %s", exc)


def contribute(
    dedupe_key: str, record: dict[str, Any], *, kind: str = DEFAULT_KIND
) -> dict[str, Any]:
    """Queue a contribution durably and flush it in the background.

    Silent and non-blocking; never raises.  The enqueue is synchronous (the
    durability guarantee — the row is on disk before we return); the send runs
    on a daemon thread.  If that send fails (offline, crash), the row persists
    and is retried by the next drain.  Gated on the kind's registered opt-in
    gate — an opted-out kind enqueues nothing.
    """
    try:
        entry = _senders.get(kind)
        gate = entry[1] if entry else None
        if gate is not None and not gate():
            return {"opted_out": True, "queued": False}
        newly = enqueue(dedupe_key, record, kind=kind)
        threading.Thread(
            target=_safe_drain, daemon=True, name="kiln-community-outbox-drain"
        ).start()
        return {"queued": newly}
    except Exception as exc:  # noqa: BLE001 — contribution must never break the caller
        logger.debug("community contribute failed (non-fatal): %s", exc)
        return {"queued": False, "error": str(exc)}


def status() -> dict[str, int]:
    """Maintainer-only health view: contribution counts by state (across all
    kinds).  Never user-facing.  ``stuck`` > 0 means sends are failing past
    _MAX_ATTEMPTS — investigate the federation endpoint, not the user."""
    with _db_lock:
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
