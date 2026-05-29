"""Tests for kiln.community_outbox — durable, idempotent community contributions.

The whole point of the outbox: a contribution is invisible to the user, so it
must never *silently* drop.  These tests pin that guarantee — a failed send
stays queued and a later drain lands it — plus idempotency, exception safety,
the stuck cap, and the opt-out gate.
"""
from __future__ import annotations

from unittest import mock

import pytest


@pytest.fixture()
def ob(tmp_path, monkeypatch):
    """Point the outbox at a temp DB and reset the cached connection."""
    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    import kiln.community_outbox as _ob

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
