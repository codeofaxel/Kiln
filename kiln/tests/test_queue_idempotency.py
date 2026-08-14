"""Tests for queue-submission idempotency.

The guarantee under test: a caller that submits a job, loses the
response, and blindly retries the identical submission with the same
``idempotency_key`` gets the ORIGINAL job back instead of queuing a
second physical print.  (GitHub issue #112 — the queue accepted the
job but the caller could not know.)

Covers, per the issue's own regression list plus house additions:
- sequential duplicate calls (in-memory and SQLite)
- concurrent calls racing one key
- restart recovery (live job, and terminal job evicted from memory)
- conflicting-payload reuse fails closed
- keyless submissions unchanged
- safety paths (cancel) never consult the key
- the MCP ``submit_job`` tool's replay/conflict response shapes,
  including the free-tier cap not refusing a replay
- ``submit_split_plan`` threading per-part keys
"""

from __future__ import annotations

import threading

import pytest

from kiln.queue import (
    IdempotencyConflict,
    JobStatus,
    PrintQueue,
    SubmitResult,
)

SUBMISSION = dict(
    file_name="bracket_v3.gcode",
    printer_name="voron",
    submitted_by="mcp-agent",
    priority=0,
)


# ---------------------------------------------------------------------------
# Core queue behaviour
# ---------------------------------------------------------------------------


class TestSubmitIdempotency:
    def test_duplicate_key_returns_original_job_in_memory(self):
        q = PrintQueue()
        first = q.submit_result(idempotency_key="k1", **SUBMISSION)
        second = q.submit_result(idempotency_key="k1", **SUBMISSION)
        assert isinstance(first, SubmitResult)
        assert first.replayed is False
        assert second.replayed is True
        assert second.job.id == first.job.id
        assert q.pending_count() == 1

    def test_duplicate_key_returns_original_job_sqlite(self, tmp_path):
        q = PrintQueue(db_path=str(tmp_path / "queue.db"))
        a = q.submit(idempotency_key="k1", **SUBMISSION)
        b = q.submit(idempotency_key="k1", **SUBMISSION)
        assert a == b
        assert q.pending_count() == 1

    def test_different_keys_queue_independent_jobs(self):
        q = PrintQueue()
        a = q.submit(idempotency_key="k1", **SUBMISSION)
        b = q.submit(idempotency_key="k2", **SUBMISSION)
        assert a != b
        assert q.pending_count() == 2

    def test_keyless_submissions_unchanged(self):
        # No key = today's behaviour exactly: every call queues.
        q = PrintQueue()
        a = q.submit(**SUBMISSION)
        b = q.submit(**SUBMISSION)
        assert a != b
        assert q.pending_count() == 2

    def test_concurrent_same_key_yields_one_job(self):
        q = PrintQueue()
        barrier = threading.Barrier(8)
        results: list[str] = []
        lock = threading.Lock()

        def racer() -> None:
            barrier.wait()
            job_id = q.submit(idempotency_key="race", **SUBMISSION)
            with lock:
                results.append(job_id)

        threads = [threading.Thread(target=racer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(results)) == 1
        assert q.pending_count() == 1

    def test_restart_recovery_replays_live_job(self, tmp_path):
        db = str(tmp_path / "queue.db")
        q1 = PrintQueue(db_path=db)
        original = q1.submit(idempotency_key="k1", **SUBMISSION)

        q2 = PrintQueue(db_path=db)  # simulated crash + restart
        replay = q2.submit_result(idempotency_key="k1", **SUBMISSION)
        assert replay.replayed is True
        assert replay.job.id == original
        assert q2.pending_count() == 1

    def test_replay_after_completion_still_dedups(self, tmp_path):
        # _reload_from_db drops terminal jobs from memory; the key must
        # still resolve via the database, and the answer is the finished
        # job — not a fresh print.
        db = str(tmp_path / "queue.db")
        q1 = PrintQueue(db_path=db)
        job_id = q1.submit(idempotency_key="k1", **SUBMISSION)
        q1.mark_starting(job_id)
        q1.mark_printing(job_id)
        q1.mark_completed(job_id)

        q2 = PrintQueue(db_path=db)
        replay = q2.submit_result(idempotency_key="k1", **SUBMISSION)
        assert replay.replayed is True
        assert replay.job.id == job_id
        assert replay.job.status is JobStatus.COMPLETED
        assert q2.pending_count() == 0

    def test_conflicting_payload_fails_closed(self):
        q = PrintQueue()
        q.submit(idempotency_key="k1", **SUBMISSION)
        with pytest.raises(IdempotencyConflict) as excinfo:
            q.submit(
                idempotency_key="k1",
                file_name="other.gcode",
                printer_name="voron",
                submitted_by="mcp-agent",
                priority=0,
            )
        assert excinfo.value.idempotency_key == "k1"
        assert q.pending_count() == 1

    def test_cross_instance_key_race_resolves_to_one_row(self, tmp_path):
        # Two PrintQueue instances over one queue.db (two processes in
        # real life).  The second instance cannot see the first's memory,
        # so the partial unique index is what refuses the duplicate row —
        # and the loser adopts the winner's job as a replay.
        db = str(tmp_path / "queue.db")
        q1 = PrintQueue(db_path=db)
        q2 = PrintQueue(db_path=db)
        a = q1.submit(idempotency_key="k1", **SUBMISSION)
        b = q2.submit(idempotency_key="k1", **SUBMISSION)
        assert a == b

    def test_cancel_works_on_keyed_job(self):
        # Safety and control paths never consult the key: a replayed or
        # keyed job cancels exactly like any other.
        q = PrintQueue()
        job_id = q.submit(idempotency_key="k1", **SUBMISSION)
        cancelled = q.cancel(job_id)
        assert cancelled.status is JobStatus.CANCELLED

    def test_find_by_idempotency_key(self):
        q = PrintQueue()
        job_id = q.submit(idempotency_key="k1", **SUBMISSION)
        found = q.find_by_idempotency_key("k1")
        assert found is not None and found.id == job_id
        assert q.find_by_idempotency_key("missing") is None


# ---------------------------------------------------------------------------
# MCP tool surface — the door agents actually knock on
# ---------------------------------------------------------------------------


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    def publish(self, event) -> None:
        self.events.append(event)


@pytest.fixture
def tool_env(monkeypatch):
    """Wire queue_tools.submit_job to a fresh queue and a stub server."""
    import kiln.server as srv

    q = PrintQueue()
    bus = _Bus()
    monkeypatch.setattr(srv, "_check_auth", lambda scope: None)
    monkeypatch.setattr(srv, "_get_queue", lambda: q)
    monkeypatch.setattr(srv, "_event_bus", bus, raising=False)
    return q, bus


class TestSubmitJobTool:
    def test_new_submission_reports_queued(self, tool_env):
        from kiln.plugins.queue_tools import submit_job

        q, bus = tool_env
        result = submit_job("part.gcode", idempotency_key="k1")
        assert result["success"] is True
        assert result["submission"] == "queued"
        assert len(bus.events) == 1

    def test_replay_reports_replayed_and_skips_event(self, tool_env):
        from kiln.plugins.queue_tools import submit_job

        q, bus = tool_env
        first = submit_job("part.gcode", idempotency_key="k1")
        second = submit_job("part.gcode", idempotency_key="k1")
        assert second["success"] is True
        assert second["submission"] == "replayed"
        assert second["job_id"] == first["job_id"]
        assert second["job_state"] == "queued"
        # The original submission published JOB_QUEUED; the replay must not.
        assert len(bus.events) == 1
        assert q.pending_count() == 1

    def test_conflict_is_a_structured_refusal(self, tool_env):
        from kiln.plugins.queue_tools import submit_job

        submit_job("part.gcode", idempotency_key="k1")
        result = submit_job("other.gcode", idempotency_key="k1")
        assert result["success"] is False
        assert result["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    def test_free_tier_cap_never_refuses_a_replay(self, tool_env, monkeypatch):
        # The original job is already counted against the cap; telling
        # its retry "queue full" would be a wrong answer about a job
        # that is in the queue.
        from kiln.plugins import queue_tools
        from kiln.plugins.queue_tools import submit_job

        # Patched on the plugin, not on ``kiln.licensing``: that module
        # ships with kiln-pro, so naming it here made the free-tier cap
        # testable only on installs that are not on the free tier.
        monkeypatch.setattr(queue_tools, "_is_free_tier", lambda: True)
        monkeypatch.setattr(queue_tools, "_free_tier_queue_cap", lambda: 1)

        first = submit_job("part.gcode", idempotency_key="k1")
        assert first["submission"] == "queued"

        # Cap reached: a genuinely new job is refused...
        blocked = submit_job("other.gcode")
        assert blocked["success"] is False
        assert blocked["code"] == "FREE_TIER_LIMIT"

        # ...but the replay of the counted job is not.
        replay = submit_job("part.gcode", idempotency_key="k1")
        assert replay["success"] is True
        assert replay["submission"] == "replayed"
        assert replay["job_id"] == first["job_id"]


# ---------------------------------------------------------------------------
# Split-plan door — per-part keys derive from the caller's plan key
# ---------------------------------------------------------------------------


class TestSplitPlanKeys:
    @staticmethod
    def _plan():
        from kiln.job_splitter import SplitJob, SplitPlan

        parts = [
            SplitJob(
                part_id=f"part_{i}",
                file_path=f"part_{i}.gcode",
                printer_name=f"printer_{i}",
                printer_model="generic",
                estimated_time_seconds=600,
                material="PLA",
                settings={},
                status="pending",
            )
            for i in range(2)
        ]
        return SplitPlan(
            original_file="widget.stl",
            split_type="multi_copy",
            parts=parts,
            total_printers=2,
            estimated_total_time_seconds=600,
            estimated_sequential_time_seconds=1200,
            time_savings_percentage=50.0,
            assembly_instructions=None,
        )

    @pytest.fixture
    def stub_persistence(self, monkeypatch):
        # The plan-row INSERT is explicitly non-fatal in the function;
        # a raising stub keeps the test off the real ~/.kiln database.
        import kiln.persistence as persistence

        class _NoDb:
            def __getattr__(self, name):
                raise RuntimeError("no db in test")

        monkeypatch.setattr(persistence, "get_db", lambda: _NoDb())

    def test_retry_with_same_key_does_not_duplicate_parts(
        self, monkeypatch, stub_persistence
    ):
        import kiln.server as srv
        from kiln.job_splitter import submit_split_plan

        q = PrintQueue()
        monkeypatch.setattr(srv, "_get_queue", lambda: q)

        submit_split_plan(self._plan(), idempotency_key="plan-key")
        submit_split_plan(self._plan(), idempotency_key="plan-key")
        assert q.pending_count() == 2  # two parts, not four

        keyed = [j.idempotency_key for j in q.list_jobs(status=JobStatus.QUEUED)]
        assert sorted(keyed) == ["plan-key:part:part_0", "plan-key:part:part_1"]

    def test_no_key_preserves_existing_behaviour(
        self, monkeypatch, stub_persistence
    ):
        import kiln.server as srv
        from kiln.job_splitter import submit_split_plan

        q = PrintQueue()
        monkeypatch.setattr(srv, "_get_queue", lambda: q)

        submit_split_plan(self._plan())
        submit_split_plan(self._plan())
        assert q.pending_count() == 4  # unkeyed: every call queues
