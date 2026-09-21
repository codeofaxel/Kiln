"""Regression: queue tools must resolve the queue lazily via ``_get_queue()``.

The REST / local-admin server (``run_rest_server``) never initialises the raw
``kiln.server._queue`` module global, so every queue tool that read it directly
crashed with ``'NoneType' object has no attribute ...``.  ``queue_summary`` was
the reported case; ``submit_job``, ``job_status``, ``cancel_queued_job``,
``cancel_queued_jobs`` and ``job_history`` shared the bug.  The fix routes every
access through ``kiln.server._get_queue()`` (the lazy initialiser every other
plugin already uses), so the tools self-initialise regardless of server context.
"""

from __future__ import annotations

import inspect

import kiln.server as mod
from kiln.events import EventBus
from kiln.plugins import queue_tools
from kiln.queue import PrintQueue


def _fresh_server_context(monkeypatch):
    """Simulate the REST/local-admin server: the raw _queue global is None."""
    monkeypatch.setattr(mod, "_queue", None)
    monkeypatch.setattr(mod, "_event_bus", EventBus())
    # In-memory queue via the lazy accessor, so the test never touches ~/.kiln.
    q = PrintQueue()
    monkeypatch.setattr(mod, "_get_queue", lambda: q)
    return q


def test_queue_summary_succeeds_when_raw_queue_global_is_none(monkeypatch):
    _fresh_server_context(monkeypatch)
    result = queue_tools.queue_summary()
    assert result["success"] is True, result
    assert "counts" in result


def test_a_first_submit_with_no_bus_yet_still_queues_and_publishes(monkeypatch, tmp_path):
    """The bus has the same lazy accessor as the queue, and the same trap:
    ``submit_job`` published straight through the raw ``_event_bus`` global,
    so the first queue tool a fresh server context ran died with
    ``'NoneType' object has no attribute 'publish'``."""
    from kiln.events import EventType

    q = _fresh_server_context(monkeypatch)
    monkeypatch.setattr(mod, "_event_bus", None)
    monkeypatch.setattr(mod, "_event_subs_wired", True)  # no watchdog wiring in a unit test
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")  # the gate is not the subject here
    result = queue_tools.submit_job("part.gcode", printer_name="garage")
    assert result["success"] is True, result
    bus = mod._get_event_bus()
    assert mod._event_bus is bus  # created on first use, then shared
    seen = []
    bus.subscribe(EventType.JOB_CANCELLED, seen.append)
    cancelled = queue_tools.cancel_queued_job(result["job_id"])
    assert cancelled["success"] is True, cancelled
    assert [e.data["job_id"] for e in seen] == [result["job_id"]]
    assert q.get_job(result["job_id"]) is not None


def test_queue_tools_never_read_the_raw_queue_global():
    """Structural guard: a stray raw ``_srv._queue`` access would reintroduce
    the crash.  ``_srv._get_queue()`` does not contain the substring
    ``_srv._queue`` (it is ``_srv._get_queue``), so this is a precise check."""
    src = inspect.getsource(queue_tools)
    assert "_srv._queue" not in src, (
        "queue_tools must use the lazy _srv._get_queue() accessor, never the "
        "raw _srv._queue global (None in the REST/local-admin server)."
    )
    assert "_srv._event_bus" not in src, (
        "queue_tools must use the lazy _srv._get_event_bus() accessor, never the "
        "raw _srv._event_bus global (None until something else asked for it)."
    )
