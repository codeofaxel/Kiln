"""Tests for the ``cancel_queued_jobs`` MCP tool (bulk queue cancellation).

Mirrors the real-PrintQueue + EventBus pattern the queue-tool tests in
test_server.py use: a fresh in-memory queue is monkeypatched onto
kiln.server, so these exercise the real scheduler and state machine rather
than a hand-rolled fake.
"""

from __future__ import annotations

import kiln.server as mod
from kiln.events import EventBus, EventType
from kiln.plugins.queue_tools import cancel_queued_jobs
from kiln.queue import JobStatus, PrintQueue


def _wire(monkeypatch):
    q = PrintQueue()
    bus = EventBus()
    monkeypatch.setattr(mod, "_queue", q)
    monkeypatch.setattr(mod, "_event_bus", bus)
    return q, bus


def _submit(q, n, printer_name=None):
    return [
        q.submit(file_name=f"j{i}.gcode", printer_name=printer_name, submitted_by="test")
        for i in range(n)
    ]


def test_dry_run_previews_without_cancelling(monkeypatch):
    q, bus = _wire(monkeypatch)
    ids = _submit(q, 3)
    r = cancel_queued_jobs(dry_run=True)
    assert r["success"] is True
    assert r["dry_run"] is True
    assert r["count"] == 3
    assert sorted(r["cancelled"]) == sorted(ids)
    assert r["skipped"] == []
    # Nothing actually changed.
    assert all(q.get_job(i).status == JobStatus.QUEUED for i in ids)
    assert not any(e.type == EventType.JOB_CANCELLED for e in bus.recent_events())


def test_cancels_all_queued_and_emits_events(monkeypatch):
    q, bus = _wire(monkeypatch)
    ids = _submit(q, 3)
    r = cancel_queued_jobs()
    assert r["success"] is True
    assert r["dry_run"] is False
    assert r["count"] == 3
    assert sorted(r["cancelled"]) == sorted(ids)
    assert r["skipped"] == []
    assert all(q.get_job(i).status == JobStatus.CANCELLED for i in ids)
    cancel_events = [
        e for e in bus.recent_events() if e.type == EventType.JOB_CANCELLED
    ]
    assert len(cancel_events) == 3
    assert all(e.data.get("bulk") is True for e in cancel_events)


def test_printer_name_scopes_the_sweep(monkeypatch):
    q, _ = _wire(monkeypatch)
    p1 = _submit(q, 2, printer_name="p1")
    p2 = _submit(q, 1, printer_name="p2")
    r = cancel_queued_jobs(printer_name="p1")
    assert sorted(r["cancelled"]) == sorted(p1)
    assert q.get_job(p2[0]).status == JobStatus.QUEUED  # other printer untouched
    assert "p1" in r["message"]


def test_running_print_is_not_a_target(monkeypatch):
    """A job already PRINTING when the sweep starts is excluded by the
    QUEUED snapshot — it is never cancelled."""
    q, _ = _wire(monkeypatch)
    a, running, c = _submit(q, 3)
    q.mark_starting(running)
    q.mark_printing(running)

    r = cancel_queued_jobs()
    assert sorted(r["cancelled"]) == sorted([a, c])
    assert q.get_job(running).status == JobStatus.PRINTING  # untouched
    assert q.get_job(a).status == JobStatus.CANCELLED
    assert q.get_job(c).status == JobStatus.CANCELLED


def test_skips_job_that_started_after_snapshot(monkeypatch):
    """The re-check: a job QUEUED at snapshot but racing into PRINTING before
    the loop reaches it is skipped, never cancelled — the running print is
    left alone."""
    q, bus = _wire(monkeypatch)
    a, b, c = _submit(q, 3)  # all QUEUED → all in the snapshot

    real_get = q.get_job

    def racing_get(job_id):
        job = real_get(job_id)
        # First time we re-check `b`, simulate it having just started.
        if job_id == b and job.status == JobStatus.QUEUED:
            q.mark_starting(b)
            q.mark_printing(b)
            return real_get(b)
        return job

    monkeypatch.setattr(q, "get_job", racing_get)

    r = cancel_queued_jobs()
    assert sorted(r["cancelled"]) == sorted([a, c])
    assert [s["job_id"] for s in r["skipped"]] == [b]
    assert "PRINTING" in r["skipped"][0]["reason"]
    assert q.get_job(b).status == JobStatus.PRINTING  # never cancelled
    # No JOB_CANCELLED event for the skipped running job.
    cancelled_ids = {
        e.data.get("job_id")
        for e in bus.recent_events()
        if e.type == EventType.JOB_CANCELLED
    }
    assert b not in cancelled_ids


def test_cancel_error_is_isolated(monkeypatch):
    """A still-queued job whose cancel() raises lands in `skipped`; the rest
    still cancel."""
    q, _ = _wire(monkeypatch)
    a, b, c = _submit(q, 3)

    real_cancel = q.cancel

    def flaky_cancel(job_id):
        if job_id == b:
            raise RuntimeError("transient failure")
        return real_cancel(job_id)

    monkeypatch.setattr(q, "cancel", flaky_cancel)

    r = cancel_queued_jobs()
    assert r["success"] is True
    assert sorted(r["cancelled"]) == sorted([a, c])
    assert [s["job_id"] for s in r["skipped"]] == [b]
    assert "could not be cancelled" in r["skipped"][0]["reason"]


def test_empty_queue_is_a_clean_noop(monkeypatch):
    q, _ = _wire(monkeypatch)
    r = cancel_queued_jobs()
    assert r["success"] is True
    assert r["count"] == 0
    assert r["cancelled"] == []
    assert r["skipped"] == []
    assert "No queued jobs to cancel" in r["message"]
