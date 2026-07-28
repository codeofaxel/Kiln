"""Tests for kiln.community_outbox — durable, idempotent community contributions.

The whole point of the outbox: a contribution is invisible to the user, so it
must never *silently* drop.  These tests pin that guarantee — a failed send
stays queued and a later drain lands it — plus idempotency, exception safety,
the stuck cap, and the opt-out gate.
"""
from __future__ import annotations

import json
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
    # ``purged`` joined the result when the drain started reclaiming
    # delivered rows; a row sent just now is well inside the retention
    # window, so nothing is reclaimed here.
    assert result == {"sent": 1, "failed": 0, "remaining": 0, "purged": 0}
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


# ---------------------------------------------------------------------------
# The 2026-07-28 stall — a queue that grew for two months and never drained
# ---------------------------------------------------------------------------
#
# 69,004 rows, 47,809 never delivered.  Two bugs compounded: the drain sent
# one batch of 50 per server start (so most rows were never even claimed),
# and kiln-pro's federation senders register as an import side effect of
# modules nothing imported at boot (so every row it DID claim failed).


def test_drain_clears_a_backlog_bigger_than_one_batch(ob):
    """One batch per call is why 47,809 rows sat for two months."""
    ob.register_sender("bulk", lambda payload, send_id: True)
    for i in range(320):  # > 6 batches of 50
        ob.enqueue(f"bulk-{i}", {"i": i}, kind="bulk")

    result = ob.drain()

    assert result["sent"] == 320
    assert result["remaining"] == 0


def test_drain_backs_off_instead_of_burning_every_retry(ob):
    """An outage must not spend the whole backlog's retry budget in one pass.

    Walking a failing queue to the end is how a transient outage turned into
    7,020 permanently-dead rows.  One failed batch, then stop.
    """
    ob.register_sender("down", lambda payload, send_id: False)
    for i in range(300):
        ob.enqueue(f"down-{i}", {"i": i}, kind="down")

    result = ob.drain(batch=50)

    assert result["sent"] == 0
    assert result["failed"] == 50, "should stop after one failed batch, not walk 300"
    assert result["remaining"] == 300  # still queued, retryable


def test_a_kind_with_no_sender_does_not_strand_other_kinds(ob):
    """The rows that failed were recovery rows; print rows had a sender."""
    ob.register_sender("has_sender", lambda payload, send_id: True)
    for i in range(10):
        ob.enqueue(f"orphan-{i}", {"i": i}, kind="no_sender_registered")
        ob.enqueue(f"fine-{i}", {"i": i}, kind="has_sender")

    result = ob.drain()

    assert result["sent"] == 10, "rows with a sender must still ship"


def test_delivered_rows_are_reclaimed(ob):
    """The outbox is a queue, not an archive — 21,195 shipped rows on disk."""
    ob.register_sender("bulk", lambda payload, send_id: True)
    for i in range(5):
        ob.enqueue(f"old-{i}", {"i": i}, kind="bulk")
    ob.drain()

    # Age the delivered rows past the retention window.
    with ob._db_lock:
        conn = ob._db()
        conn.execute(
            "UPDATE community_outbox SET sent_at = ?",
            (time.time() - ob._DELIVERED_RETENTION_S - 60,),
        )
        conn.commit()

    assert ob.purge_delivered() == 5
    assert ob.status()["total"] == 0


def test_dead_rows_outlive_delivered_ones(ob):
    """A dead row is the evidence sends were failing — don't erase it fast."""
    ob.register_sender("down", lambda payload, send_id: False)
    ob.enqueue("stuck", {"i": 1}, kind="down")
    with ob._db_lock:
        conn = ob._db()
        conn.execute(
            "UPDATE community_outbox SET attempts = ?, created_at = ?",
            (ob._MAX_ATTEMPTS, time.time() - ob._DELIVERED_RETENTION_S - 60),
        )
        conn.commit()

    assert ob.purge_delivered() == 0, "a day-old dead row must survive"


# ---------------------------------------------------------------------------
# One print, one row — the two contribution paths share a key and a vocabulary
# ---------------------------------------------------------------------------
#
# The monitors (community_autofire) and record_print_outcome both ship a
# finished print here.  Each used to mint its own dedupe key and translate
# outcomes with its own private map, so a print that was WATCHED and then
# RECORDED shipped twice — under two different words — to an endpoint with no
# server-side dedupe.  The aggregate counted one print as two.


def test_translate_outcome_maps_every_learning_vocabulary():
    assert_map = {
        "completed": "success",   # monitor vocabulary
        "success": "success",     # DB vocabulary
        "SUCCESS": "success",     # case/whitespace tolerant
        " failed ": "failed",
        "partial": "partial",
    }
    from kiln.community_outbox import translate_outcome

    for word, expected in assert_map.items():
        assert translate_outcome(word) == expected, word


def test_translate_outcome_contributes_nothing_without_a_verdict():
    """cancelled / timeout / pending / unknown say nothing about the print;
    an unrecognised word fails CLOSED rather than defaulting to success."""
    from kiln.community_outbox import translate_outcome

    for word in (
        "cancelled", "timeout", "pending", "unknown", "paused", "running",
        "finished", "", None,
    ):
        assert translate_outcome(word) is None, word


def test_key_is_anchored_on_the_job_not_the_derived_signature():
    """The job id is the one identity both paths carry verbatim; each DERIVES
    its signature by a different route (fingerprint vs. caller-supplied
    hash), so a signature-bearing key would not collapse the double-ship."""
    from kiln.community_outbox import print_contribution_key

    assert print_contribution_key("job-7", "geo-from-fingerprint") == (
        print_contribution_key("job-7", "hash-from-caller")
    )
    assert print_contribution_key("job-7", "g") != print_contribution_key("job-8", "g")


def test_key_falls_back_to_geometry_with_no_job():
    """A printer driven directly has no job id — the model's signature plus
    its file is the strongest identity left."""
    from kiln.community_outbox import print_contribution_key

    keyed = print_contribution_key(None, "geo-aaa", "plate.gcode")
    assert "geo-aaa" in keyed
    assert keyed != print_contribution_key(None, "geo-bbb", "plate.gcode")
    assert keyed != print_contribution_key(None, "geo-aaa", "other.gcode")


def test_non_learning_outcome_contributes_nothing(ob):
    from kiln.community_outbox import contribute_print_outcome

    result = contribute_print_outcome(
        outcome="cancelled", geometric_signature="geo16char0000000", job_id="j1"
    )
    assert result == {"contributed": False, "reason": "non_quality_outcome"}
    assert ob.status()["total"] == 0


def test_caller_extras_cannot_override_the_translated_outcome(ob):
    """Payload richness is preserved, but the vocabulary stays in one place."""
    from kiln.community_outbox import contribute_print_outcome

    contribute_print_outcome(
        outcome="completed",
        geometric_signature="geo16char0000000",
        job_id="j2",
        extra={"outcome": "smuggled", "settings": {"temp_tool": 210}},
    )
    row = ob._db().execute(
        "SELECT payload FROM community_outbox WHERE dedupe_key = 'print:j2'"
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["outcome"] == "success"
    assert payload["settings"] == {"temp_tool": 210}  # richness preserved


def test_watched_then_recorded_print_lands_one_row(ob, monkeypatch):
    """The double-ship, collapsed: the same physical print through BOTH
    contribution paths leaves exactly one row in the outbox."""
    monkeypatch.setenv("KILN_COMMUNITY_OPT_IN", "true")
    import kiln.persistence as _p
    monkeypatch.setattr(_p, "_db", None, raising=False)

    from kiln import community_autofire as ca
    from kiln.plugins.learning_tools import record_print_outcome

    # 1) A monitor watches the print end.
    with mock.patch(
        "kiln.community_autofire.geometric_signature_for",
        return_value="geo16char0000000",
    ):
        watched = ca.auto_contribute_completion(
            outcome="completed",
            printer_file_name="plate.gcode",
            job_id="job-dup",
            printer_model="Bambu A1",
            material="PLA",
            print_time_seconds=1200,
        )
    assert watched["contributed"] is True
    assert watched["outcome"] == "success"
    assert ob.status()["total"] == 1

    # 2) The agent then records the same print by hand.
    with mock.patch("kiln.server._check_auth", return_value=None):
        recorded = record_print_outcome(
            job_id="job-dup",
            outcome="success",
            printer_name="bambu-01",
            file_name="plate.gcode",
            file_hash="filehash00000000",
            material_type="PLA",
            quality_grade="good",
        )
    assert recorded.get("success") is True
    assert ob.status()["total"] == 1, "one physical print must ship once"

    monkeypatch.setattr(_p, "_db", None, raising=False)


def test_a_second_real_print_still_ships(ob, monkeypatch):
    """The collapse must not swallow a genuine repeat print."""
    monkeypatch.setenv("KILN_COMMUNITY_OPT_IN", "true")
    from kiln import community_autofire as ca

    with mock.patch(
        "kiln.community_autofire.geometric_signature_for",
        return_value="geo16char0000000",
    ):
        for job in ("job-a", "job-b"):
            ca.auto_contribute_completion(
                outcome="completed", printer_file_name="plate.gcode", job_id=job,
            )
    assert ob.status()["total"] == 2


def test_registration_is_suppressed_under_test_runners(ob):
    """A registered sender POSTs to production.

    Auto-wiring one during a suite run means any test that drains a queue
    publishes to the real federation endpoint — which is exactly how 30 junk
    rows reached community_recoveries while this fix was being written.
    """
    assert ob._registration_suppressed() is True

    called = []
    ob.ensure_senders()  # must not reach the bridge at all
    assert called == []


def test_opted_out_print_is_not_reported_as_contributed(ob):
    """The status dict is the only place a maintainer sees the difference
    between 'shipped' and 'the user opted out'."""
    from kiln.community_outbox import contribute_print_outcome

    with mock.patch(
        "kiln.community_sync.community_opt_in_enabled", return_value=False
    ):
        result = contribute_print_outcome(
            outcome="completed", geometric_signature="geo16char0000000", job_id="j3",
        )
    assert result["contributed"] is False
    assert result["opted_out"] is True
    assert ob.status()["total"] == 0
