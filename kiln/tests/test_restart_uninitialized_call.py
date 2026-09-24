"""``restart_server`` keeps the MCP connection it restarts under.

``restart_server`` re-execs over the running process, and ``os.execve`` keeps
the stdio pipe: the host sees no disconnect, repeats no ``initialize``
handshake, and its next request lands on a fresh process that has not been
initialized.  Both SDK majors refused that with JSON-RPC boilerplate that
reads as a bad argument.  Measured 2026-09-23, twice: the agent blamed the
parameter it had passed (``detail``, then ``printer_name``), dropped it, and
retried; neither parameter had changed in any commit between the restarts.

The fresh process now takes up the handshake the pipe already made, and
refuses — in words naming the restart — only when none was handed down.
Behavioural on the SDK that is installed: every request below goes through
the SDK's real server loop on memory streams, and ``restart_server`` runs
with only its final exec intercepted.
"""

from __future__ import annotations

import json
import os
import threading

import anyio
import pytest

from kiln import mcp_compat
from kiln.mcp_compat import (
    MCP_SDK_MAJOR,
    RESTART_HANDSHAKE_ENV,
    RESTART_MARKER_ENV,
    FastMCP,
    host_can_ask_the_user,
    install_uninitialized_request_guard,
    lowlevel_server,
    restart_keeps_connection,
    stamp_restart,
    uninitialized_request_message,
)

SDK_TEXT = "Invalid request parameters"

_CLIENT = {
    "protocolVersion": "2025-06-18",
    "capabilities": {"elicitation": {}},
    "clientInfo": {"name": "probe-host", "version": "9.9"},
}


@pytest.fixture(autouse=True)
def _no_connection(monkeypatch):
    monkeypatch.delenv(RESTART_HANDSHAKE_ENV, raising=False)
    monkeypatch.delenv(RESTART_MARKER_ENV, raising=False)
    monkeypatch.setattr(mcp_compat, "_handshake", None)
    monkeypatch.setattr(mcp_compat, "_inherited", None)


def _server() -> object:
    mcp = FastMCP("restart-probe")

    @mcp.tool()
    def can_ask(ctx: mcp_compat.Context) -> str:
        """Whether this connection's client declared it can ask the person."""
        return "yes" if host_can_ask_the_user(mcp, ctx) else "no"

    assert install_uninitialized_request_guard(mcp)
    return mcp


def _session_message(raw: dict):
    from mcp import types
    from mcp.shared.message import SessionMessage

    if MCP_SDK_MAJOR >= 2:
        from pydantic import TypeAdapter

        return SessionMessage(message=TypeAdapter(types.JSONRPCMessage).validate_python(raw))
    return SessionMessage(types.JSONRPCMessage.model_validate(raw))


def _drive(mcp: object, messages: list[dict], replies: int) -> list[dict]:
    """Send *messages* into the SDK's real server loop and collect the first
    *replies* responses (anything carrying an ``id``)."""
    server = lowlevel_server(mcp)

    async def _run() -> list[dict]:
        c2s_send, c2s_recv = anyio.create_memory_object_stream(32)
        s2c_send, s2c_recv = anyio.create_memory_object_stream(32)
        out: list[dict] = []
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.run, c2s_recv, s2c_send, server.create_initialization_options())
            for raw in messages:
                await c2s_send.send(_session_message(raw))
            with anyio.fail_after(10):
                while len(out) < replies:
                    msg = (await s2c_recv.receive()).message
                    msg = getattr(msg, "root", msg)
                    data = msg.model_dump(mode="json", by_alias=True, exclude_none=True)
                    if "id" in data:
                        out.append(data)
            tg.cancel_scope.cancel()
        return out

    return anyio.run(_run)


def _call(request_id: int = 7) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": "can_ask", "arguments": {}},
    }


def _text(reply: dict) -> str:
    assert "result" in reply, reply
    return reply["result"]["content"][0]["text"]


class TestTheWords:
    def test_after_a_restart_the_message_names_it_and_clears_the_parameters(self, monkeypatch):
        monkeypatch.setenv(RESTART_MARKER_ENV, "2026-09-23T16:57:26-07:00")
        msg = uninitialized_request_message("tools/call")
        assert "restarted at 16:57:26" in msg
        assert "restart_server" in msg
        assert "parameters you passed were not the problem" in msg
        assert "Retry it unchanged once" in msg
        assert "Kiln MCP server needs reconnecting in the app" in msg
        assert SDK_TEXT not in msg

    def test_without_a_restart_it_still_names_the_handshake(self):
        msg = uninitialized_request_message("tools/call")
        assert "initialize handshake" in msg
        assert "restarted" not in msg

    def test_a_request_that_is_not_a_tool_call_is_named_by_its_method(self):
        assert "this resources/read request never ran" in uninitialized_request_message("resources/read")

    def test_an_unparseable_marker_is_shown_rather_than_hidden(self, monkeypatch):
        monkeypatch.setenv(RESTART_MARKER_ENV, "just now")
        assert "restarted at just now" in uninitialized_request_message()


class TestTheConnectionSurvivesARestart:
    """Through the real server loop: a handed-down handshake is taken up,
    capabilities and all; without one the refusal names the restart."""

    def test_a_handed_down_handshake_is_taken_up_capabilities_and_all(self, monkeypatch):
        monkeypatch.setenv(RESTART_HANDSHAKE_ENV, json.dumps({"params": _CLIENT, "protocol_version": "2025-06-18"}))
        mcp = _server()
        (reply,) = _drive(mcp, [_call()], 1)
        assert _text(reply) == "yes", "the client's declared capabilities must survive the restart"
        assert RESTART_HANDSHAKE_ENV not in os.environ, "taken once, never inherited further"
        assert restart_keeps_connection(), "the next restart must hand it on again"

    def test_without_a_handshake_the_refusal_names_the_restart(self, monkeypatch):
        monkeypatch.setenv(RESTART_MARKER_ENV, "2026-09-23T12:22:44-07:00")
        mcp = _server()
        (reply,) = _drive(mcp, [_call()], 1)
        error = reply.get("error")
        assert error, reply
        assert error["message"] != SDK_TEXT
        assert "restarted at 12:22:44" in error["message"]
        assert "parameters you passed were not the problem" in error["message"]

    def test_an_initialize_is_recorded_and_the_restart_hands_it_on(self, monkeypatch):
        mcp = _server()
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": _CLIENT}
        done = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        first, second = _drive(mcp, [init, done, _call(2)], 2)
        assert _text(second) == "yes"
        env = stamp_restart({})
        handed = json.loads(env[RESTART_HANDSHAKE_ENV])
        assert handed["params"]["clientInfo"]["name"] == "probe-host"
        assert handed["protocol_version"] == first["result"]["protocolVersion"]
        # ...and a fresh process given that environment keeps the connection.
        monkeypatch.setattr(mcp_compat, "_handshake", None)
        monkeypatch.setenv(RESTART_HANDSHAKE_ENV, env[RESTART_HANDSHAKE_ENV])
        (reply,) = _drive(_server(), [_call(3)], 1)
        assert _text(reply) == "yes"

    def test_a_client_that_handshakes_afresh_is_served_as_usual(self, monkeypatch):
        stale = dict(_CLIENT, capabilities={})
        monkeypatch.setenv(RESTART_HANDSHAKE_ENV, json.dumps({"params": stale, "protocol_version": "2025-06-18"}))
        mcp = _server()
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": _CLIENT}
        done = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        _first, second = _drive(mcp, [init, done, _call(2)], 2)
        assert _text(second) == "yes", "a new handshake wins over the handed-down one"

    def test_an_unreadable_handshake_is_ignored_and_the_refusal_still_explains(self, monkeypatch):
        monkeypatch.setenv(RESTART_HANDSHAKE_ENV, "{not json")
        (reply,) = _drive(_server(), [_call()], 1)
        assert "initialize handshake" in reply["error"]["message"]


class TestRestartServerHandsTheConnectionOn:
    """``restart_server`` itself, with only its final exec intercepted — it
    would otherwise replace pytest."""

    @staticmethod
    def _restart(monkeypatch) -> tuple[dict, dict]:
        from kiln import server

        seen: dict = {}
        execed = threading.Event()

        def _fake_execve(path, argv, env):
            seen.update(env)
            execed.set()

        monkeypatch.setattr(os, "execve", _fake_execve)
        monkeypatch.setattr(server, "_flush_restart_stdio", lambda: 0)
        monkeypatch.delenv(server._SERVE_WRAPPER_ENV, raising=False)
        result = server.restart_server(clean_env=False)
        assert execed.wait(5), "the restart never reached its exec"
        return result, seen

    def test_a_connection_with_a_handshake_is_kept(self, monkeypatch):
        monkeypatch.setattr(mcp_compat, "_handshake", {"params": _CLIENT, "protocol_version": "2025-06-18"})
        result, env = self._restart(monkeypatch)
        assert result["keeps_connection"] is True
        assert json.loads(env[RESTART_HANDSHAKE_ENV])["params"]["clientInfo"]["name"] == "probe-host"
        assert env[RESTART_MARKER_ENV]
        assert "keeps its connection" in result["message"]
        assert "will drop" not in result["message"], "execve keeps the pipe; nothing drops"

    def test_without_a_handshake_the_result_says_the_first_call_may_be_refused(self, monkeypatch):
        result, env = self._restart(monkeypatch)
        assert result["keeps_connection"] is False
        assert RESTART_HANDSHAKE_ENV not in env
        assert "retry it unchanged" in result["message"]


def test_installing_twice_adds_one_layer():
    mcp = FastMCP("guard-probe")
    assert install_uninitialized_request_guard(mcp)
    assert install_uninitialized_request_guard(mcp)
    if MCP_SDK_MAJOR >= 2:
        chain = lowlevel_server(mcp).middleware
        assert sum(1 for m in chain if getattr(m, mcp_compat._GUARDED, False)) == 1
    else:
        from mcp.server.session import ServerSession

        assert getattr(ServerSession, mcp_compat._GUARDED, False)
