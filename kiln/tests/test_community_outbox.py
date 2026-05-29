"""Tests for kiln.community_outbox — durable, idempotent community contributions.

The whole point of the outbox: a contribution is invisible to the user, so it
must never *silently* drop.  These tests pin that guarantee — a failed send
stays queued and a later drain lands it — plus idempotency, exception safety,
the stuck cap, and the opt-out gate.
"""
from __future__ import annotations

import threading
import time
from unittest import mock

import pytest


@pytest.fixture()
def ob(tmp_path, monkeypatch):
    """Point the outbox at a temp DB and reset the cached connection."""
    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    import kiln.community_outbox as _ob
    import kiln.community_sync as _cs

    # Safety net: a contribute()-spawned background drain can outlive a test's
    # own mock context.  Default the community sender to a no-op so such a
    # straggler never makes a REAL network call (which would block _drain_lock
    # on a 5s timeout and bleed into the next test).  Tests that assert send
    # behavior still patch sync_community_print inside their own `with`; on
    # exit they restore to this no-op, not the live network call.
    monkeypatch.setattr(_cs, "sync_community_print", lambda *a, **k: False)

    _ob.close()  # drop any connection cached by another test
    yield _ob
    _ob.close()


def _rec(sig: str = "abc") -> dict:
    return {
        "geometric_signature": sig,
        "printer_model": "bambu_a1",
        "material": "PLA",
        "outcome": "success",
    }


def test_enqueue_then_pending(ob):
    assert ob.enqueue("k1", _rec()) is True
    assert ob.status()["pending"] == 1
    assert ob.status()["sent"] == 0


def test_enqueue_is_idempotent(ob):
    assert ob.enqueue("k1", _rec()) is True
    assert ob.enqueue("k1", _rec()) is False  # duplicate dedupe_key ignored
    assert ob.status()["total"] == 1


def test_drain_sends_and_marks_sent(ob):
    ob.enqueue("k1", _rec())
    with mock.patch("kiln.community_sync.sync_community_print", return_value=True) as send:
        result = ob.drain()
    send.assert_called_once()
    assert result == {"sent": 1, "failed": 0, "remaining": 0}
    assert ob.status() == {"pending": 0, "sent": 1, "stuck": 0, "total": 1}


def test_failed_send_stays_queued_not_dropped(ob):
    ob.enqueue("k1", _rec())
    with mock.patch("kiln.community_sync.sync_community_print", return_value=False):
        result = ob.drain()
    assert result["sent"] == 0
    assert result["failed"] == 1
    assert result["remaining"] == 1
    assert ob.status()["pending"] == 1  # still queued for retry — NOT silently dropped


def test_failed_then_retry_lands(ob):
    ob.enqueue("k1", _rec())
    with mock.patch("kiln.community_sync.sync_community_print", return_value=False):
        ob.drain()
    assert ob.status()["pending"] == 1
    # network recovers — the next drain lands it (the durability guarantee)
    with mock.patch("kiln.community_sync.sync_community_print", return_value=True):
        ob.drain()
    assert ob.status() == {"pending": 0, "sent": 1, "stuck": 0, "total": 1}


def test_send_exception_is_caught_and_requeued(ob):
    ob.enqueue("k1", _rec())
    with mock.patch(
        "kiln.community_sync.sync_community_print", side_effect=RuntimeError("boom")
    ):
        result = ob.drain()  # must not raise
    assert result["failed"] == 1
    assert ob.status()["pending"] == 1


def test_stuck_after_max_attempts(ob):
    ob.enqueue("k1", _rec())
    with mock.patch("kiln.community_sync.sync_community_print", return_value=False):
        for _ in range(ob._MAX_ATTEMPTS):
            ob.drain()
    s = ob.status()
    assert s["pending"] == 0
    assert s["stuck"] == 1  # capped — not retried forever, surfaced to maintainers


def test_contribute_respects_opt_out(ob):
    with mock.patch("kiln.community_sync.community_opt_in_enabled", return_value=False):
        result = ob.contribute("k1", _rec())
    assert result == {"opted_out": True, "queued": False}
    assert ob.status()["total"] == 0  # opted-out enqueues nothing


def test_contribute_opted_in_enqueues_durably(ob):
    with mock.patch("kiln.community_sync.community_opt_in_enabled", return_value=True), \
         mock.patch("kiln.community_sync.sync_community_print", return_value=True):
        result = ob.contribute("k1", _rec())
    assert result["queued"] is True
    # The enqueue is synchronous (durable) regardless of the background drain's
    # timing — the row is on disk the moment contribute() returns.
    assert ob.status()["total"] == 1


def test_enqueue_not_blocked_by_in_flight_drain(ob):
    """The durability contract: enqueue must never block behind a drain's
    network send.  This fails on the old design that held one lock across the
    whole drain (including I/O) — a print-completion handler calling
    contribute() would stall for the send's full timeout."""
    ob.enqueue("k1", _rec())
    in_send = threading.Event()
    release = threading.Event()

    def slow_send(_rec, send_id=None):
        in_send.set()
        release.wait(timeout=5)
        return True

    with mock.patch("kiln.community_sync.sync_community_print", side_effect=slow_send):
        drainer = threading.Thread(target=ob.drain)
        drainer.start()
        try:
            assert in_send.wait(timeout=2), "drain never reached the network send"
            # A fresh enqueue while the send is mid-flight must return promptly.
            started = time.monotonic()
            ob.enqueue("k2", _rec("xyz"))
            assert time.monotonic() - started < 1.0  # ~5s if the lock spanned I/O
        finally:
            release.set()
            drainer.join(timeout=5)
    assert ob.status()["total"] == 2


def test_concurrent_drain_skips_to_avoid_double_send(ob):
    """Only one drain works the queue at a time.  A second drain entered while
    the first is mid-send returns sent=0 instead of re-sending the same row —
    the receiver has no server-side dedupe, so a double-send = double-count."""
    ob.enqueue("k1", _rec())
    in_send = threading.Event()
    release = threading.Event()

    def slow_send(_rec, send_id=None):
        in_send.set()
        release.wait(timeout=5)
        return True

    with mock.patch(
        "kiln.community_sync.sync_community_print", side_effect=slow_send
    ) as send:
        drainer = threading.Thread(target=ob.drain)
        drainer.start()
        try:
            assert in_send.wait(timeout=2)
            second = ob.drain()  # in-flight drain holds _drain_lock
            assert second["sent"] == 0
        finally:
            release.set()
            drainer.join(timeout=5)
    send.assert_called_once()  # the row was sent exactly once
    assert ob.status() == {"pending": 0, "sent": 1, "stuck": 0, "total": 1}


def test_stuck_row_warns_maintainer(ob, caplog):
    """Crossing the attempt cap must emit one WARNING — a down endpoint should
    show up in the logs, not just in a status() count nobody calls."""
    ob.enqueue("k1", _rec())
    with mock.patch("kiln.community_sync.sync_community_print", return_value=False), \
         caplog.at_level("WARNING", logger="kiln.community_outbox"):
        for _ in range(ob._MAX_ATTEMPTS):
            ob.drain()
    stuck_warnings = [r for r in caplog.records if "stuck" in r.getMessage().lower()]
    assert len(stuck_warnings) == 1  # exactly once, on the crossing
    assert ob.status()["stuck"] == 1


def test_enqueue_assigns_a_send_id(ob):
    """Every queued row carries a random server-idempotency token."""
    ob.enqueue("k1", _rec())
    conn = ob._db()
    send_id = conn.execute(
        "SELECT send_id FROM community_outbox WHERE dedupe_key = 'k1'"
    ).fetchone()["send_id"]
    assert send_id and len(send_id) >= 16


def test_send_id_is_stable_across_retries(ob):
    """The server-dedupe token must be identical on every (re)send of a row, so
    a crash-replayed contribution folds into one server row rather than many.
    (dedupe_key guards local double-queue; send_id guards server double-insert.)"""
    ob.enqueue("k1", _rec())
    seen: list[str | None] = []

    def capture(_payload, send_id=None):
        seen.append(send_id)
        return False  # fail so the row stays queued and is retried

    with mock.patch("kiln.community_sync.sync_community_print", side_effect=capture):
        ob.drain()
        ob.drain()
    assert len(seen) == 2
    assert seen[0] and seen[0] == seen[1]  # same stable, non-empty token


# ---------------------------------------------------------------------------
# Kind-aware sender registry (federation generalization)
# ---------------------------------------------------------------------------


def test_kind_dispatch_routes_to_registered_sender(ob):
    """A contribution's ``kind`` selects its registered sender; the random
    send_id is handed to that sender for endpoint-side idempotency."""
    calls: list = []
    ob.register_sender("test_kind", lambda p, sid: (calls.append((p, sid)) or True))
    try:
        ob.enqueue("k1", {"x": 1}, kind="test_kind")
        result = ob.drain()
        assert result["sent"] == 1
        assert len(calls) == 1
        assert calls[0][0] == {"x": 1}
        assert calls[0][1]  # send_id passed through to the kind's sender
    finally:
        ob._senders.pop("test_kind", None)


def test_contribute_kind_gate_blocks_when_opted_out(ob):
    """Each kind carries its own opt-in gate; an opted-out kind enqueues
    nothing (the community kind keeps its community_opt_in gate)."""
    ob.register_sender("gated_kind", lambda p, sid: True, gate=lambda: False)
    try:
        result = ob.contribute("k1", {"x": 1}, kind="gated_kind")
        assert result == {"opted_out": True, "queued": False}
        assert ob.status()["total"] == 0
    finally:
        ob._senders.pop("gated_kind", None)


def test_unknown_kind_counts_as_failed_not_silently_looped(ob):
    """A row whose kind has no registered sender (e.g. kiln-pro not loaded) is
    treated as a failed send — retried, then surfaced as stuck — never a silent
    no-op loop."""
    ob.enqueue("k1", {"x": 1}, kind="no_such_kind")
    result = ob.drain()
    assert result["sent"] == 0
    assert result["failed"] == 1
    assert ob.status()["pending"] == 1
