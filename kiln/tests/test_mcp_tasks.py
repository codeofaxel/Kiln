"""A wait on a print, answered as an MCP task to a host that runs tools in the background.

Driven through a real SDK client on the 2026-07-28 wire, against Kiln's own
server and its real ``await_print_completion``, with a fake printer: the
client declares the tasks extension the way Claude Code does, takes the
handle, polls ``tasks/get`` and reads the result from it (Claude Code never
calls ``tasks/result``).  A client that declares nothing gets the blocking
call it always got.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal
from unittest import mock

import pytest

from kiln.printers.base import JobProgress, PrinterCapabilities, PrinterState, PrinterStatus

mcp_client = pytest.importorskip("mcp.client.extension")
mcp_types = pytest.importorskip("mcp_types")

from mcp import Client  # noqa: E402
from mcp.client.extension import ClientExtension, ResultClaim  # noqa: E402
from mcp_types import (  # noqa: E402
    CallToolResult,
    CancelTaskRequest,
    CancelTaskRequestParams,
    GetTaskRequest,
    GetTaskRequestParams,
    Result,
)
from pydantic import TypeAdapter  # noqa: E402

from kiln import mcp_tasks, server, watch_plan  # noqa: E402

_RAW = TypeAdapter(dict[str, Any])


class _Handle(Result):
    result_type: Literal["task"] = "task"
    task_id: str
    status: str
    ttl_ms: int | None = None
    created_at: str
    last_updated_at: str


class _TasksClient(ClientExtension):
    """The client half of the extension, recording every wire state it saw."""

    identifier = mcp_tasks.TASKS_EXTENSION_ID

    def __init__(self, *, cancel_after_polls: int | None = None) -> None:
        self.handles: list[_Handle] = []
        self.polls: list[dict[str, Any]] = []
        self._cancel_after = cancel_after_polls

    def claims(self) -> list[ResultClaim[Any]]:
        return [ResultClaim(result_type="task", model=_Handle, resolve=self._resolve)]

    async def _resolve(self, handle: _Handle, ctx: Any) -> CallToolResult:
        self.handles.append(handle)
        while True:
            raw = await ctx.session.send_request(
                GetTaskRequest(params=GetTaskRequestParams(task_id=handle.task_id)), _RAW
            )
            self.polls.append(raw)
            if raw["status"] == "completed":
                return CallToolResult.model_validate(raw["result"])
            if raw["status"] in ("failed", "cancelled"):
                return CallToolResult(content=[], is_error=True)
            if self._cancel_after is not None and len(self.polls) >= self._cancel_after:
                await ctx.session.send_request(
                    CancelTaskRequest(params=CancelTaskRequestParams(task_id=handle.task_id)), _RAW
                )
            await asyncio.sleep(0.05)


def _state(run: PrinterStatus) -> PrinterState:
    return PrinterState(connected=True, state=run)


def _printer(*, printing_reads: int | None) -> mock.MagicMock:
    """A printer that prints for ``printing_reads`` reads, then sits idle.

    ``None`` prints forever.  Completion climbs 10 points a read.
    """
    a = mock.MagicMock()
    a.name = "test-printer"
    a.capabilities = PrinterCapabilities(can_snapshot=False)
    reads = {"n": 0}

    def get_state() -> PrinterState:
        reads["n"] += 1
        if printing_reads is not None and reads["n"] > printing_reads:
            return _state(PrinterStatus.IDLE)
        return _state(PrinterStatus.PRINTING)

    a.get_state.side_effect = get_state
    a.get_job.side_effect = lambda: JobProgress(
        completion=min(99.0, 10.0 * reads["n"]), file_name="plate_1.3mf"
    )
    a.reads = reads
    return a


@pytest.fixture(autouse=True)
def _tasks_on_kilns_server(monkeypatch: pytest.MonkeyPatch):
    """Kiln's own server with the tasks layer in, a short grace and fast polls."""
    from kiln.server import _tool_limiter

    monkeypatch.setattr(_tool_limiter, "check", lambda *a, **kw: None)
    monkeypatch.setattr(mcp_tasks, "SYNC_GRACE_S", 0.3)
    assert mcp_tasks.install(server.mcp)
    mcp_tasks._reset_for_tests()
    yield
    mcp_tasks._reset_for_tests()


def _call(extensions: list[ClientExtension], args: dict[str, Any]) -> CallToolResult:
    async def go() -> CallToolResult:
        async with Client(server.mcp, extensions=extensions) as client:
            assert client.protocol_version in mcp_tasks._modern_versions()
            return await client.call_tool("await_print_completion", args)

    return asyncio.run(go())


def _outcome(result: CallToolResult) -> str:
    import json

    return json.loads(result.content[0].text)["outcome"]


def test_the_server_says_it_runs_tasks() -> None:
    async def go() -> Any:
        async with Client(server.mcp) as client:
            return client.server_capabilities.extensions

    assert mcp_tasks.TASKS_EXTENSION_ID in (asyncio.run(go()) or {})


def test_a_long_wait_becomes_a_task_that_says_how_far_the_print_is() -> None:
    printer = _printer(printing_reads=3)
    host = _TasksClient()
    with mock.patch("kiln.server._get_adapter", return_value=printer):
        result = _call([host], {"timeout": 30, "poll_interval": 1})

    assert len(host.handles) == 1, (
        "a host that runs tasks gets a handle, not a blocked call; it got: "
        + (result.content[0].text[:400] if result.content else repr(result))
    )
    handle = host.handles[0]
    assert handle.status == "working" and handle.task_id
    # Every poll is the spec's tasks/get shape; the last carries the result.
    assert all(p["resultType"] == "complete" and "ttlMs" in p for p in host.polls)
    assert host.polls[-1]["status"] == "completed"
    assert _outcome(result) == "completed"
    # While it ran, the host was told how far the print was, and that
    # closing the app ends the wait, never the print.
    lines = [p.get("statusMessage") or "" for p in host.polls if p["status"] == "working"]
    assert any("% printed" in line for line in lines), lines
    assert all("not the print" in line for line in lines if "% printed" in line)


def test_a_host_that_runs_no_tasks_gets_the_blocking_call() -> None:
    printer = _printer(printing_reads=3)
    with mock.patch("kiln.server._get_adapter", return_value=printer):
        result = _call([], {"timeout": 30, "poll_interval": 1})

    assert _outcome(result) == "completed"
    assert not mcp_tasks._tasks, "no task is made for a host that did not ask for one"


def test_a_print_already_finished_answers_directly() -> None:
    printer = _printer(printing_reads=0)
    host = _TasksClient()
    with mock.patch("kiln.server._get_adapter", return_value=printer):
        result = _call([host], {"timeout": 30, "poll_interval": 1})

    assert host.handles == [], "a wait that ends inside the grace window costs no task"
    assert _outcome(result) == "completed"


def test_stopping_the_task_stops_the_wait_and_leaves_the_print_alone() -> None:
    printer = _printer(printing_reads=None)  # prints forever
    host = _TasksClient(cancel_after_polls=3)
    with mock.patch("kiln.server._get_adapter", return_value=printer):
        _call([host], {"timeout": 15, "poll_interval": 1})
        assert host.polls[-1]["status"] == "cancelled"
        # The wait's own loop has to notice and end, not poll on for its
        # full ten minutes in a thread nobody is listening to.
        deadline = time.time() + 3
        while time.time() < deadline and mcp_tasks._running:
            time.sleep(0.05)
        reads_after_stop = printer.reads["n"]
        time.sleep(2.5)
    assert printer.reads["n"] == reads_after_stop, "the wait kept polling after it was stopped"
    printer.cancel_print.assert_not_called()
    printer.pause_print.assert_not_called()


def test_an_unknown_task_is_refused_with_the_code_the_spec_names() -> None:
    from mcp.shared.exceptions import MCPError

    async def go() -> MCPError | None:
        async with Client(server.mcp, extensions=[_TasksClient()]) as client:
            try:
                await client.session.send_request(
                    GetTaskRequest(params=GetTaskRequestParams(task_id="kiln-task-nope")), _RAW
                )
            except MCPError as exc:
                return exc
        return None

    refused = asyncio.run(go())
    assert refused is not None and refused.error.code == -32602


def test_a_result_wrapper_installed_later_still_sees_the_tool_result() -> None:
    """The tasks layer stays outermost: a mutator added after it lands inside."""
    from kiln.mcp_compat import FastMCP, wrap_call_tool_result

    fresh = FastMCP("probe")

    @fresh.tool()
    def await_print_completion() -> dict:
        time.sleep(0.6)
        return {"outcome": "completed"}

    assert mcp_tasks.install(fresh)
    seen: list[str] = []
    assert wrap_call_tool_result(fresh, lambda result, ctx, name: seen.append(type(result).__name__))

    host = _TasksClient()

    async def go() -> CallToolResult:
        async with Client(fresh, extensions=[host]) as client:
            return await client.call_tool("await_print_completion", {})

    assert _outcome(asyncio.run(go())) == "completed"
    assert host.handles, "the call became a task"
    assert seen == ["CallToolResult"], "the mutator saw the tool's own result, not a task handle"


def test_the_server_installs_tasks_last_at_startup() -> None:
    """``kiln serve`` is the one door, and the layer must go in after every
    result wrapper, or the first of them installed later would sit outside it
    and see a task handle where it expects a tool result."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(server._start)))
    installs = [
        node.func.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "install"
        and isinstance(node.func.value, ast.Name)
    ]
    assert installs and installs[-1] == "mcp_tasks", installs


class TestTheWatchPlanInTheBackground:
    def test_a_background_wait_wakes_the_agent_once_per_look(self) -> None:
        blocking = watch_plan.build_watch_plan()
        background = watch_plan.build_watch_plan(background=True)
        assert background["background"] is True and blocking["background"] is False
        assert background["cost"]["wakeups"] == len(background["checkpoints"])
        assert background["cost"]["wakeups"] < blocking["cost"]["wakeups"]
        assert "background" in background["how"]

    def test_keeping_watch_to_the_end_costs_the_look_cadence(self) -> None:
        plan = watch_plan.build_watch_plan(background=True, estimated_s=6 * 3600)
        assert plan["cost"]["wakeups_per_hour_if_continuous"] == 3600 // watch_plan.THEN_EVERY_S
        assert f"about every {watch_plan.THEN_EVERY_S // 60} minutes" in plan["say_once"]

    def test_a_print_started_from_a_task_host_gets_the_background_plan(self) -> None:
        from kiln.print_start_verdict import _watch_plan

        token = mcp_tasks._HOST_RUNS_TASKS.set(True)
        try:
            assert _watch_plan(None, "p.3mf")["background"] is True
        finally:
            mcp_tasks._HOST_RUNS_TASKS.reset(token)
        assert _watch_plan(None, "p.3mf")["background"] is False
