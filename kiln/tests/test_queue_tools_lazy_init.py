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


def test_queue_tools_never_read_the_raw_queue_global():
    """Structural guard: a stray raw ``_srv._queue`` access would reintroduce
    the crash.  ``_srv._get_queue()`` does not contain the substring
    ``_srv._queue`` (it is ``_srv._get_queue``), so this is a precise check."""
    src = inspect.getsource(queue_tools)
    assert "_srv._queue" not in src, (
        "queue_tools must use the lazy _srv._get_queue() accessor, never the "
        "raw _srv._queue global (None in the REST/local-admin server)."
    )
