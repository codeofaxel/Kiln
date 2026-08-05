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

# One drain call keeps claiming batches until the queue is empty or it has
# moved this many rows.  A ceiling, not a target: it bounds one startup
# drain's work so a huge backlog can't hold the thread forever, while still
# clearing thousands of rows per run instead of 50.
_DRAIN_MAX_ROWS = 5000

# The outbox is a queue, not an archive.  A delivered row is dead weight the
# moment it ships; a row that exhausted its retries is evidence of an outage,
# so it is kept far longer before being reclaimed.
_DELIVERED_RETENTION_S = 24 * 60 * 60          # 1 day
_DEAD_RETENTION_S = 30 * 24 * 60 * 60          # 30 days

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


def _default_db_path() -> str:
    """Where the REAL per-user outbox lives, ignoring any override."""
    return os.path.join(os.path.expanduser("~"), ".kiln", "community_outbox.db")


def _db_path() -> str:
    """Co-locate the outbox with kiln.db, honoring the KILN_DB_PATH override
    (so tests can point it at a tmp dir)."""
    base = os.environ.get("KILN_DB_PATH") or os.path.join(
        os.path.expanduser("~"), ".kiln", "kiln.db"
    )
    return os.path.join(os.path.dirname(base) or ".", "community_outbox.db")


def _suppressed_under_test() -> bool:
    """True when a test/CI runner would otherwise touch the REAL outbox.

    The outbox is the one local store that reaches OTHER PEOPLE: queued
    rows are sent to the shared community corpus, which every user reads
    for "what worked for people like you".  A suite that enqueues into
    the real file therefore doesn't just pollute one machine — it ships
    fabricated prints and recoveries to everybody (2026-07-28: 48,523
    fixture rows — ``strategy_a``, ``voron_2_4_350``, ``sig123`` — were
    found queued on a developer machine, 21,536 of them already sent,
    against exactly ONE real print).

    Same shape as ``daily_stats._recording_suppressed``, applied at BOTH
    ends: nothing is enqueued and nothing is sent.  A test that points
    ``KILN_DB_PATH`` at its own directory is asking to exercise the
    outbox and is never suppressed — only the real per-user file is
    protected.
    """
    if _db_path() != _default_db_path():
        return False
    try:
        from kiln.heartbeat import _is_ci_environment

        return _is_ci_environment()
    except Exception:  # noqa: BLE001 — heartbeat absent, fall back to env
        return any(
            os.environ.get(var) for var in ("CI", "PYTEST_CURRENT_TEST")
        )


def _hosted_aggregate_process() -> bool:
    """True on the shared multi-tenant server, where contributions lie.

    One process serving every tenant is not a user's machine: its
    ``~/.kiln`` conflates all callers, so a row it contributes carries
    nobody's real print.  The heartbeat has refused to send from this
    process since it shipped (``heartbeat.py``, same env flag); the
    community wire never got the check, leaving ``POST /api/tools/*``
    callers able to enqueue into the Fly box's outbox and publish to the
    shared corpus.  Same flag, same refusal, both ends of the queue.
    """
    return os.environ.get("KILN_HOSTED_MULTITENANT") == "1"


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
    drain's network I/O.

    No-ops under a test/CI runner still pointed at the real per-user
    outbox — see :func:`_suppressed_under_test` — and on the hosted
    multi-tenant server, whose outbox is nobody's machine."""
    if _suppressed_under_test() or _hosted_aggregate_process():
        return False
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


def ensure_senders() -> None:
    """Give every contribution kind a chance to register its sender.

    A sender is registered as an import side effect of the module that owns
    the kind, and kiln-pro's federation modules are imported lazily — inside
    tool bodies — so a drain that ran at startup found no sender for their
    kinds and failed every row it claimed.  Asking for registration before
    draining is the fix; leaving it to import order is what broke.

    No-op when kiln-pro isn't installed: free installs have exactly the one
    built-in sender and nothing to add.

    Suppressed under a test/CI runner, on the ``daily_stats`` precedent: a
    registered sender POSTs to the real federation endpoint, so auto-wiring
    one during a suite run would let any test that drains a queue publish to
    production. (It did: a drain written to verify this very fix put 30 junk
    rows in ``community_recoveries`` on 2026-07-28.) A test that wants a
    sender registers its own stub — which is what a test should be asserting
    against anyway.
    """
    if _registration_suppressed():
        logger.debug("community outbox: sender registration suppressed (test/CI)")
        return
    try:
        from kiln_pro.bridge import pro_features

        pro_features.register_community_senders()
    except Exception as exc:  # noqa: BLE001 — no kiln-pro, or a broken module
        logger.debug("community outbox: pro senders unavailable: %s", exc)


def _registration_suppressed() -> bool:
    """True when a test/CI runner would otherwise wire up REAL senders.

    Same env list as :func:`kiln.daily_stats._recording_suppressed`, applied
    at the registration side.
    """
    try:
        from kiln.heartbeat import _is_ci_environment

        return _is_ci_environment()
    except Exception:  # noqa: BLE001 — heartbeat absent, fall back to env
        return any(os.environ.get(v) for v in ("CI", "PYTEST_CURRENT_TEST"))


def purge_delivered(retain_seconds: float = _DELIVERED_RETENTION_S) -> int:
    """Drop rows that are done with — delivered, or dead past the retry cap.

    The outbox is a QUEUE, not an archive: nothing read these rows again once
    they shipped, but nothing deleted them either, so the file grew forever
    (2026-07-28: 21,195 delivered rows and 7,020 dead ones still on disk,
    25 MB in a folder that is supposed to hold the user's work).

    Dead rows are kept far longer than delivered ones — they are the evidence
    that sends were failing, and deleting them promptly would erase the only
    on-disk trace of an outage.
    """
    cutoff = time.time() - retain_seconds
    dead_cutoff = time.time() - _DEAD_RETENTION_S
    try:
        with _db_lock:
            conn = _db()
            cur = _retry_on_locked(
                lambda: conn.execute(
                    "DELETE FROM community_outbox "
                    "WHERE (sent_at IS NOT NULL AND sent_at < ?) "
                    "   OR (sent_at IS NULL AND attempts >= ? AND created_at < ?)",
                    (cutoff, _MAX_ATTEMPTS, dead_cutoff),
                )
            )
            removed = cur.rowcount or 0
            _retry_on_locked(conn.commit)
        if removed:
            logger.debug("community outbox: purged %d finished row(s)", removed)
        return removed
    except Exception as exc:  # noqa: BLE001 — housekeeping never breaks a drain
        logger.debug("community outbox purge failed: %s", exc)
        return 0


def drain(batch: int = _DRAIN_BATCH, *, max_rows: int = _DRAIN_MAX_ROWS) -> dict[str, int]:
    """Send queued contributions, dispatching each to its kind's sender.

    Drains REPEATEDLY until the queue is empty, this run has moved
    ``max_rows``, or a whole batch fails.  One batch per call was the other
    half of the 2026-07-28 stall: at 50 rows per server start, a 47,809-row
    backlog needed ~956 restarts to clear while new rows kept arriving, so
    the oldest rows were rechewed forever and most were never claimed once.

    Stopping on a fully-failed batch is deliberate: when the endpoint is
    down or the machine is offline, walking the whole backlog would burn
    every row's retry budget in a single pass — exactly how a transient
    outage turned into 7,020 permanently dead rows.  Progress continues,
    failure backs off.

    Best-effort: a failed send stays queued (attempts incremented) for the next
    drain.  Never raises.  Returns ``{sent, failed, remaining, purged}``.

    Concurrency: the network send runs with NO lock held, so a slow/offline
    send never blocks ``enqueue``.  ``_db_lock`` is taken only to claim the
    batch and to record each row's result (committed per row, so a crash
    replays at most one row).  ``_drain_lock`` admits a single drain at a time;
    a second concurrent caller returns ``sent=0`` immediately.
    """
    if _suppressed_under_test() or _hosted_aggregate_process():
        # A test runner pointed at the REAL outbox: ensure_senders() below
        # imports the live senders, which POST to the shared production
        # corpus.  Refuse — a suite must never publish to other users.
        # The hosted multi-tenant box refuses for the same reason from the
        # other side: its rows describe no real machine.
        return {"sent": 0, "failed": 0, "remaining": 0, "purged": 0}
    try:
        from kiln.community_sync import network_sends_suppressed

        wire_guarded = network_sends_suppressed()
    except Exception:  # noqa: BLE001 — guard unavailable, fall through to send
        wire_guarded = False
    if wire_guarded:
        # The check above protects the real per-user DB and stands down for
        # a custom KILN_DB_PATH — which is exactly how the 2026-08-05 leak
        # ran: a suite with a relocated HOME exercised the outbox "safely"
        # and the drain published its fixtures to production.  DB isolation
        # and network safety are different questions; this one is asked at
        # the chokepoint every sender KIND passes through, so a sender
        # registered next year is covered without knowing this exists.
        # (``KILN_COMMUNITY_TEST_SEND=1`` is the explicit escape for tests
        # that exercise the drain with a mocked sender.)
        return {"sent": 0, "failed": 0, "remaining": _count_pending(), "purged": 0}
    if not _drain_lock.acquire(blocking=False):
        # Another drain owns the queue right now — don't double-send.
        return {"sent": 0, "failed": 0, "remaining": _count_pending(), "purged": 0}
    try:
        ensure_senders()
        sent = failed = 0
        while sent + failed < max_rows:
            passed, missed = _drain_one_batch(batch)
            sent += passed
            failed += missed
            if passed == 0:
                # Nothing moved: either the queue is empty or sends are
                # failing.  Either way, stop — see the docstring.
                break
        purged = purge_delivered()
        return {
            "sent": sent,
            "failed": failed,
            "remaining": _count_pending(),
            "purged": purged,
        }
    finally:
        _drain_lock.release()


def _drain_one_batch(batch: int) -> tuple[int, int]:
    """Claim and send at most *batch* rows.  Returns ``(sent, failed)``.

    Assumes the caller holds ``_drain_lock``.
    """
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

    return sent, failed

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


# ---------------------------------------------------------------------------
# Print-outcome contributions — ONE key, ONE vocabulary, both callers
# ---------------------------------------------------------------------------
#
# Two paths ship a finished print to the community pool: the monitors
# (``community_autofire``, watching a print end) and ``record_print_outcome``
# (an agent recording it).  They each carried their own dedupe key and their
# own outcome map — the monitor said ``completed``, the tool said ``success``
# — so a print that was WATCHED and then RECORDED shipped twice, under two
# different words, to a federation endpoint with no server-side dedupe.  The
# aggregate counted one print as two.
#
# The fix is not a check in either caller: it is that neither caller mints a
# key or translates a word any more.  Both call
# :func:`contribute_print_outcome`, which owns both, and the outbox's
# existing dedupe-by-key collapses the pair.

#: Every outcome vocabulary that reaches the community pool, mapped to the DB
#: vocabulary in exactly one place.  Absent from this map ⇒ contributes
#: nothing (see :func:`translate_outcome`).
_OUTCOME_TRANSLATION: dict[str, str] = {
    "success": "success",
    "completed": "success",   # monitor vocabulary
    "failed": "failed",
    "partial": "partial",
}


def translate_outcome(outcome: str | None) -> str | None:
    """Map a caller's outcome word to the community DB vocabulary.

    Returns ``None`` — meaning *contribute nothing* — for every word that
    carries no verdict on the print: ``cancelled`` and ``timeout`` (clock and
    user events, not model-quality signals), ``pending`` and ``unknown`` (the
    print is still owed an answer), a mid-print state word, or a vocabulary
    this function has never heard of.  Unknown words fail CLOSED rather than
    defaulting to success: an aggregate that invents a grade is worse than one
    that never saw the print.
    """
    if not outcome:
        return None
    return _OUTCOME_TRANSLATION.get(str(outcome).strip().lower())


def canonical_printer_model(model: str | None) -> str:
    """One noun per machine model, everywhere the corpus is touched.

    The corpus aggregates BY this string: rows written as ``"Bambu A1"``
    and rows written as ``"bambu_a1"`` are the same machine that will
    never be counted together (both spellings were live in production by
    2026-08-05, in an eight-row table).  Every writer normalizes here at
    the contribution door, and the read side (the insight endpoint)
    applies the same function to its query — one definition, imported,
    never re-derived.

    ``"Bambu A1"`` → ``"bambu_a1"``; a slug already in canonical form
    passes through unchanged; empty/None → ``"unknown"`` (matching the
    door's existing fallback).
    """
    import re as _re

    slug = _re.sub(r"[^a-z0-9]+", "_", (model or "").strip().lower()).strip("_")
    return slug or "unknown"


def print_contribution_key(
    job_id: str | None,
    geometric_signature: str,
    printer_file_name: str | None = None,
) -> str:
    """The canonical dedupe key for ONE physical print.

    The **job id is the identity** whenever there is one: a job is one print
    run, and it is the single field both contribution paths carry verbatim
    (the monitor reads it off the job, the outcome tool is called with it).
    Keying on anything either path DERIVES — each computes the geometric
    signature by a different route — would leave the double-ship in place,
    which is the whole reason this key exists.

    The geometric signature identifies the MODEL, not the run, so it can only
    be the FALLBACK: a printer driven directly, with nothing in Kiln's queue,
    has no job id.  There it is paired with the printer's file name so two
    different files can't collide.  The known cost: two runs of the same file
    with no job id collapse into one contribution while the first row is still
    on disk.  That is the deliberate direction to err — this endpoint has no
    server-side dedupe, so an over-report is permanent in the aggregate while
    an under-report is one missed sample.
    """
    job = (job_id or "").strip()
    if job:
        return f"print:{job}"
    fallback = (printer_file_name or "").strip() or geometric_signature
    return f"print:sig:{fallback}:{geometric_signature}"


def contribute_print_outcome(
    *,
    outcome: str,
    geometric_signature: str,
    job_id: str | None = None,
    printer_file_name: str | None = None,
    printer_model: str | None = None,
    material: str | None = None,
    print_time_seconds: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Contribute one finished print to the community pool.

    The single door for print-outcome contributions: it translates the
    caller's vocabulary, mints the canonical dedupe key, and hands the record
    to :func:`contribute` (durable, opt-in-gated, non-blocking).

    ``extra`` carries a caller's own richer fields (settings, quality grade,
    failure mode) and is applied UNDER the fields this function owns, so no
    caller can smuggle a different outcome word or signature into the payload
    — the translation stays in one place by construction.

    :returns: ``{"contributed": False, "reason": ...}`` when the outcome
        carries no verdict, the geometry is unknown, the user is opted out,
        or the enqueue failed; else ``{"contributed": True, ...}`` merged
        with the outbox result (``queued`` False there means an identical
        print was already queued — the double-ship, collapsed).
    """
    mapped = translate_outcome(outcome)
    if mapped is None:
        return {"contributed": False, "reason": "non_quality_outcome"}
    signature = str(geometric_signature or "").strip()
    if not signature:
        return {"contributed": False, "reason": "no_geometry"}

    record: dict[str, Any] = dict(extra or {})
    record.update(
        {
            "geometric_signature": signature,
            # Normalized HERE, the one door every path passes through, so
            # the corpus never again holds two spellings of one machine.
            "printer_model": canonical_printer_model(printer_model),
            "material": material or "unknown",
            "outcome": mapped,
            "print_time_seconds": int(print_time_seconds) if print_time_seconds else 0,
        }
    )
    result = contribute(
        print_contribution_key(job_id, signature, printer_file_name), record
    )
    # A row already on disk under this key IS contributed (by the path that
    # got here first) — that is the collapse working.  Opted out or a failed
    # enqueue is not, and this dict is the only place a maintainer would ever
    # see the difference.
    contributed = not (result.get("opted_out") or result.get("error"))
    return {
        "contributed": contributed,
        "signature": signature,
        "outcome": mapped,
        **result,
    }


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
