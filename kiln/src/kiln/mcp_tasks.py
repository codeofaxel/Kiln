"""Long tool calls answered as MCP tasks, for hosts that run them in the background.

Waiting on a print is one blocking tool the agent calls again and again
(:mod:`kiln.watch_plan`), and every return is a wake-up: the agent reads its
whole conversation again and is billed for it.  A host that speaks the MCP
tasks extension (``io.modelcontextprotocol/tasks``, protocol 2026-07-28;
Claude Code's terminal app first) can instead take a task handle, keep
working, ask after the task for free, and tell the agent when it ends.  So
here a slow call that outlives a short grace window answers with a handle,
and the work goes on in the background.

The server decides per call, by the spec's own rule: only when THIS request
declared the extension in its ``_meta`` client capabilities, on a 2026-era
wire, and only for a tool in :data:`TASK_TOOLS`.  Everyone else gets the
blocking call exactly as before, which is always a valid answer.  A call
that finishes inside :data:`SYNC_GRACE_S` returns its result directly, so a
print that has already ended costs no task at all.

The layer sits outside every other ``tools/call`` wrapper
(:func:`kiln.mcp_compat.wrap_call_tool_outermost`), so what a task completes
with is the same finished result the blocking call would have returned.

What a task can say while it runs: a tool reports through :func:`say` (the
host shows the latest line) and waits through :func:`wait`, which returns
True once the host asked to stop.  Outside a task both are ordinary: ``say``
does nothing and ``wait`` sleeps.

Honest limits.  Tasks live in this process: a stdio server stops when its
host exits, and its tasks with it, whatever the host's own label says, so a
running task's line says the watch ends with the app, never that it
survives it.  No ``input_required`` (a task never asks the person
anything), no task notifications (polling is the spec default).  Cancelling
is cooperative: a tool that never calls :func:`wait` runs to its end, and
its result is dropped.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any

from kiln import mcp_compat

logger = logging.getLogger(__name__)

__all__ = [
    "TASKS_EXTENSION_ID",
    "TASK_TOOLS",
    "client_declared_tasks",
    "host_runs_tasks",
    "install",
    "say",
    "wait",
    "wire_task",
]

TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"

#: Tools whose call may become a task.  Each is here because someone decided
#: it waits or computes long enough to be worth one.
TASK_TOOLS: frozenset[str] = frozenset({"await_print_completion"})

#: A call finishing inside this window returns directly, with no task.
SYNC_GRACE_S = 2.5

#: Tasks running at once; past it a call blocks as it always did.
MAX_RUNNING = 4

#: How often the host should ask after a task (milliseconds).  A print's
#: state moves on the scale of seconds; asking faster buys nothing.
POLL_INTERVAL_MS = 5000

#: How long a task is kept after it was created (milliseconds), and how
#: many finished ones are kept at all.  The process is the store.
TTL_MS = 24 * 3600 * 1000
KEEP_FINISHED = 64

#: Said while a task runs and its tool has said nothing yet.
_RUNNING_LINE = "Running. This ends if the app running the agent is closed."

#: JSON-RPC codes the spec names.
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603
_MISSING_CAPABILITY = -32021

_TERMINAL = frozenset({"completed", "failed", "cancelled"})

#: Marks a server this module has installed on.
_INSTALLED = "_kiln_mcp_tasks_installed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _modern_versions() -> frozenset[str]:
    """Protocol versions on which a task handle can reach the host."""
    try:
        from mcp_types.version import MODERN_PROTOCOL_VERSIONS  # type: ignore[import-not-found]

        return frozenset(MODERN_PROTOCOL_VERSIONS)
    except Exception:  # noqa: BLE001 — an SDK without the constant
        return frozenset({"2026-07-28"})


def client_declared_tasks(meta: Any, protocol_version: Any) -> bool:
    """True when this request may be answered with a task handle.

    Two conditions.  The request's own ``_meta`` client capabilities name the
    tasks extension (a declaration on an earlier request does not count), and
    the wire is 2026-era: a handle is a 2026 result shape, and on an older
    wire it turns the call into an error after the tool has already run.
    """
    if not isinstance(meta, dict):
        return False
    caps = meta.get(CLIENT_CAPABILITIES_META_KEY)
    extensions = caps.get("extensions") if isinstance(caps, dict) else None
    if not (isinstance(extensions, dict) and TASKS_EXTENSION_ID in extensions):
        return False
    return str(protocol_version or "") in _modern_versions()


class _Task:
    """One task: its wire facts, what it ended with, and its stop signal."""

    def __init__(self, tool: str) -> None:
        self.task_id = "kiln-task-" + secrets.token_hex(16)
        self.tool = tool
        self.status = "working"
        self.message: str | None = _RUNNING_LINE
        self.created_at = _now()
        self.updated_at = self.created_at
        self.created_mono = time.monotonic()
        self.result: dict[str, Any] | None = None
        self.error: dict[str, Any] | None = None
        self.stop = threading.Event()


_lock = threading.Lock()
_tasks: dict[str, _Task] = {}
#: The background runs, held so the event loop cannot collect one mid-print.
_running: dict[str, asyncio.Future[Any]] = {}

#: The task the current call runs as, if any; read by :func:`say` and
#: :func:`wait`, which are called from the tool's worker thread.  Context
#: variables travel there with the call.
_CURRENT: contextvars.ContextVar[_Task | None] = contextvars.ContextVar(
    "kiln_mcp_task", default=None
)
#: Whether the host on this call can take a task, for code that plans ahead
#: (the watch plan at a print's start).
_HOST_RUNS_TASKS: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "kiln_host_runs_tasks", default=False
)


def host_runs_tasks() -> bool:
    """True when the call being served came from a host that takes tasks."""
    return _HOST_RUNS_TASKS.get()


def say(line: str) -> None:
    """Set the line a host shows for the task this call runs as.  No-op outside one."""
    task = _CURRENT.get()
    if task is None or not line:
        return
    with _lock:
        if task.status == "working":
            task.message = str(line)
            task.updated_at = _now()


def wait(seconds: float) -> bool:
    """Sleep ``seconds``; True when the host asked to stop this task meanwhile.

    Outside a task it is a plain sleep and always returns False.
    """
    task = _CURRENT.get()
    if task is None:
        time.sleep(max(0.0, seconds))
        return False
    return task.stop.wait(max(0.0, seconds))


def _finish(task: _Task, *, status: str, result: dict | None = None, error: dict | None = None,
            message: str | None = None) -> None:
    with _lock:
        if task.status in _TERMINAL:
            return  # a terminal state never changes (a stop wins over a late result)
        task.status = status
        task.result = result
        task.error = error
        task.message = message
        task.updated_at = _now()
        _running.pop(task.task_id, None)
        _forget_old()


def _forget_old() -> None:
    """Drop expired tasks, then the oldest finished ones past the keep count."""
    now = time.monotonic()
    for task_id in [t for t, task in _tasks.items() if (now - task.created_mono) * 1000 > TTL_MS]:
        if task_id not in _running:
            _tasks.pop(task_id, None)
    finished = [t for t, task in _tasks.items() if task.status in _TERMINAL]
    for task_id in finished[: max(0, len(finished) - KEEP_FINISHED)]:
        _tasks.pop(task_id, None)


def wire_task(
    *,
    result_type: str,
    task_id: str,
    status: str,
    created_at: str,
    last_updated_at: str,
    ttl_ms: int | None,
    poll_interval_ms: int,
    status_message: str | None = None,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A task as the 2026-07-28 tasks extension puts it on the wire.

    ``result_type`` is ``"task"`` for the handle a ``tools/call`` answers
    with and ``"complete"`` for a ``tasks/get`` answer.  The task's fields
    sit at the top level, and a ``tasks/get`` answer carries the outcome
    itself: the tool result under ``result`` once completed, a JSON-RPC
    error object under ``error`` once failed.  A host reads nothing else (a
    task-running host never calls ``tasks/result``).  The one builder for
    every Kiln server that answers with tasks, so the shape a host reads
    cannot differ between them.
    """
    shape: dict[str, Any] = {
        "resultType": result_type,
        "taskId": task_id,
        "status": status,
        "createdAt": created_at,
        "lastUpdatedAt": last_updated_at,
        "ttlMs": ttl_ms,
        "pollIntervalMs": poll_interval_ms,
    }
    if status_message:
        shape["statusMessage"] = status_message
    if result_type == "complete" and status == "completed" and result is not None:
        shape["result"] = result
    if result_type == "complete" and status == "failed":
        shape["error"] = error or {"code": _INTERNAL_ERROR, "message": "The call failed."}
    return shape


def _wire(task: _Task, *, result_type: str) -> dict[str, Any]:
    """The task as the spec's wire object, with whatever its state carries."""
    return wire_task(
        result_type=result_type,
        task_id=task.task_id,
        status=task.status,
        created_at=task.created_at,
        last_updated_at=task.updated_at,
        ttl_ms=TTL_MS,
        poll_interval_ms=POLL_INTERVAL_MS,
        status_message=task.message,
        result=task.result,
        error=task.error,
    )


def _as_wire_result(result: Any) -> dict[str, Any]:
    """A tool-call result as the dict it would have gone out as."""
    if isinstance(result, dict):
        return dict(result)
    dump = getattr(result, "model_dump", None)
    if callable(dump):
        return dump(by_alias=True, mode="json", exclude_none=True)
    return {"content": [{"type": "text", "text": str(result)}]}


def _error_of(exc: BaseException) -> dict[str, Any]:
    """A raised error as the JSON-RPC error object a failed task carries."""
    error = getattr(exc, "error", None)
    code = getattr(error, "code", None)
    message = getattr(error, "message", None)
    if isinstance(code, int) and message:
        return {"code": code, "message": str(message)}
    return {"code": _INTERNAL_ERROR, "message": str(exc) or type(exc).__name__}


async def _call_tool_layer(inner: Any, ctx: Any, params: Any) -> Any:
    """The outermost ``tools/call`` layer: run inline, or hand back a task."""
    declared = client_declared_tasks(getattr(ctx, "meta", None), getattr(ctx, "protocol_version", None))
    host_token = _HOST_RUNS_TASKS.set(declared)
    try:
        tool = mcp_compat.tool_call_name(params) or ""
        with _lock:
            busy = len(_running) >= MAX_RUNNING
        if not declared or tool not in TASK_TOOLS or busy:
            return await inner(ctx, params)

        task = _Task(tool)
        task_token = _CURRENT.set(task)
        try:
            # The run copies this context, so the tool's thread sees the task.
            work = asyncio.ensure_future(inner(ctx, params))
        finally:
            _CURRENT.reset(task_token)
        try:
            done, _ = await asyncio.wait({work}, timeout=SYNC_GRACE_S)
        except asyncio.CancelledError:
            work.cancel()
            raise
        if done:
            return work.result()  # a raised error goes out as it always did

        with _lock:
            _tasks[task.task_id] = task
            _running[task.task_id] = work
        work.add_done_callback(lambda fut: _settle(task, fut))
        return _wire(task, result_type="task")
    finally:
        _HOST_RUNS_TASKS.reset(host_token)


def _settle(task: _Task, fut: asyncio.Future[Any]) -> None:
    if fut.cancelled():
        _finish(task, status="cancelled", message="Stopped.")
        return
    exc = fut.exception()
    if exc is not None:
        logger.warning("mcp task %s (%s) failed: %s", task.task_id, task.tool, exc)
        _finish(task, status="failed", error=_error_of(exc), message=str(exc) or None)
        return
    _finish(task, status="completed", result=_as_wire_result(fut.result()))


def _declared_or_refuse(ctx: Any) -> None:
    """A task method answers only a client that declared the extension (spec)."""
    if client_declared_tasks(getattr(ctx, "meta", None), getattr(ctx, "protocol_version", None)):
        return
    from mcp.shared.exceptions import MCPError  # type: ignore[import-not-found]

    raise MCPError(
        code=_MISSING_CAPABILITY,
        message=f"Client did not declare required extension {TASKS_EXTENSION_ID!r}",
    )


def _known_or_refuse(task_id: str) -> _Task:
    with _lock:
        _forget_old()
        task = _tasks.get(str(task_id or ""))
    if task is None:
        from mcp.shared.exceptions import MCPError  # type: ignore[import-not-found]

        raise MCPError(code=_INVALID_PARAMS, message="Failed to retrieve task: Task not found")
    return task


async def _tasks_get(ctx: Any, params: Any) -> dict[str, Any]:
    _declared_or_refuse(ctx)
    task = _known_or_refuse(getattr(params, "task_id", ""))
    with _lock:
        return _wire(task, result_type="complete")


async def _tasks_cancel(ctx: Any, params: Any) -> dict[str, Any]:
    _declared_or_refuse(ctx)
    task = _known_or_refuse(getattr(params, "task_id", ""))
    task.stop.set()  # the tool's next wait() sees it and ends
    _finish(task, status="cancelled", message="Stopped. Anything already started keeps going.")
    return {"resultType": "complete"}


async def _tasks_update(ctx: Any, params: Any) -> dict[str, Any]:
    # No task here ever asks for input, so there is nothing to apply; the
    # spec has the server acknowledge and ignore responses it did not ask for.
    _declared_or_refuse(ctx)
    _known_or_refuse(getattr(params, "task_id", ""))
    return {"resultType": "complete"}


def install(mcp: Any) -> bool:
    """Serve tasks on this server.  True when both halves went in.

    SDK 2 only; on SDK 1 nothing changes and every call blocks as before.
    Installing twice on one server is a no-op that answers True.
    """
    if getattr(mcp, _INSTALLED, False):
        return True
    try:
        from mcp_types import (  # type: ignore[import-not-found]
            CancelTaskRequestParams,
            GetTaskRequestParams,
            RequestParams,
        )
    except ImportError:
        return False

    class _UpdateTaskParams(RequestParams):
        task_id: str
        input_responses: dict[str, Any] | None = None

    served = mcp_compat.serve_extension(
        mcp,
        TASKS_EXTENSION_ID,
        {},
        {
            "tasks/get": (GetTaskRequestParams, _tasks_get),
            "tasks/cancel": (CancelTaskRequestParams, _tasks_cancel),
            "tasks/update": (_UpdateTaskParams, _tasks_update),
        },
    )
    if not served:
        return False
    wrapped = mcp_compat.wrap_call_tool_outermost(mcp, _call_tool_layer)
    if wrapped:
        setattr(mcp, _INSTALLED, True)
    return wrapped


def _reset_for_tests() -> None:
    with _lock:
        _tasks.clear()
        _running.clear()
