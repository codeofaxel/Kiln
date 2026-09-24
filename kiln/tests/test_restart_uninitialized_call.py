"""The first call after ``restart_server`` is refused in words that name the
restart — not as "Invalid request parameters".

``restart_server`` re-execs over the running process, and ``os.execve`` keeps
the stdio pipe: the host sees no disconnect, repeats no ``initialize``
handshake, and its next ``tools/call`` lands on a fresh process that has not
been initialized.  Both SDK majors refuse that with JSON-RPC boilerplate that
reads as a bad argument.  Measured 2026-09-23, twice, in the server log:
"Received request before initialization was complete" 11 s and 18 s after
each restart; the agent blamed the parameter it had passed (``detail``,
then ``printer_name``), dropped it, and the retry only worked because the
host had re-initialized on its own meanwhile.  Neither parameter had changed
in any commit between the restarts.

Behavioural on the SDK that is installed: 1.x through a real
``ServerSession`` on memory streams, 2.x through the middleware the guard
appends.  The tool itself is never called — it execs over pytest.
"""

from __future__ import annotations

import inspect

import anyio
import pytest

from kiln import mcp_compat
from kiln.mcp_compat import (
    MCP_SDK_MAJOR,
    RESTART_MARKER_ENV,
    FastMCP,
    install_uninitialized_request_guard,
    lowlevel_server,
    stamp_restart,
    uninitialized_request_message,
)

SDK_TEXT = "Invalid request parameters"


class TestTheWords:
    def test_after_a_restart_the_message_names_it_and_clears_the_parameters(self, monkeypatch):
        monkeypatch.setenv(RESTART_MARKER_ENV, "2026-09-23T16:57:26-07:00")
        msg = uninitialized_request_message("tools/call")
        assert "restarted at 16:57:26" in msg
        assert "restart_server" in msg
        assert "parameters you passed were not the problem" in msg
        assert "Retry the same call unchanged" in msg
        assert "reconnect the Kiln MCP server" in msg
        assert SDK_TEXT not in msg

    def test_without_a_restart_it_still_names_the_handshake(self, monkeypatch):
        monkeypatch.delenv(RESTART_MARKER_ENV, raising=False)
        msg = uninitialized_request_message("tools/call")
        assert "initialize handshake" in msg
        assert "restarted" not in msg
        assert "parameters you passed were not the problem" in msg

    def test_a_request_that_is_not_a_tool_call_is_named_by_its_method(self, monkeypatch):
        monkeypatch.delenv(RESTART_MARKER_ENV, raising=False)
        assert "this resources/read request never ran" in uninitialized_request_message("resources/read")

    def test_an_unparseable_marker_is_shown_rather_than_hidden(self, monkeypatch):
        monkeypatch.setenv(RESTART_MARKER_ENV, "just now")
        assert "restarted at just now" in uninitialized_request_message()


class TestRestartServerSetsTheScene:
    def test_the_child_env_is_stamped_with_the_restart(self):
        env = stamp_restart({"PATH": "/bin"})
        assert env["PATH"] == "/bin"
        assert env[RESTART_MARKER_ENV].startswith("20")  # an ISO date

    def test_the_tool_stamps_its_child_and_no_longer_promises_a_drop(self):
        from kiln import server

        src = inspect.getsource(server.restart_server)
        assert "stamp_restart(" in src, "the fresh process must be able to name this restart"
        assert "connection will drop" not in src, "execve keeps the pipe; the old sentence was false"
        assert "_RESTART_SAME_PIPE_NOTE" in src
        assert '"first_call_may_be_refused": True' in src
        note = server._RESTART_SAME_PIPE_NOTE
        assert "does not drop" in note
        assert "retry that call unchanged" in note
        assert "parameters are not the problem" in note

    def test_the_server_installs_the_guard_at_startup(self):
        from pathlib import Path

        from kiln import server

        src = Path(server.__file__).read_text(encoding="utf-8")
        assert "install_uninitialized_request_guard(mcp)" in src


@pytest.mark.skipif(MCP_SDK_MAJOR != 1, reason="the 1.x session gate")
class TestTheRefusalOnSdk1:
    """A real ``ServerSession``, never initialized, handed a ``tools/call``."""

    @staticmethod
    def _refuse(monkeypatch, *, initialized: bool = False):
        from mcp.server.models import InitializationOptions
        from mcp.server.session import InitializationState, ServerSession
        from mcp.shared.session import RequestResponder
        from mcp.types import (
            CallToolRequest,
            CallToolRequestParams,
            ClientRequest,
            ServerCapabilities,
        )

        monkeypatch.setenv(RESTART_MARKER_ENV, "2026-09-23T12:22:44-07:00")
        assert install_uninitialized_request_guard(FastMCP("guard-probe"))

        async def _run():
            r_send, r_recv = anyio.create_memory_object_stream(8)
            w_send, w_recv = anyio.create_memory_object_stream(8)
            session = ServerSession(
                r_recv, w_send,
                InitializationOptions(server_name="kiln", server_version="0", capabilities=ServerCapabilities()),
            )
            if initialized:
                session._initialization_state = InitializationState.Initialized
            request = ClientRequest(
                CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(name="printer_status", arguments={"detail": "lite"}),
                )
            )
            responder = RequestResponder(
                request_id=7, request_meta=None, request=request, session=session,
                on_complete=lambda _r: None,
            )
            await session._received_request(responder)
            written = None
            with anyio.move_on_after(0.2):
                written = await w_recv.receive()
            return responder, written

        return anyio.run(_run)

    def test_it_answers_with_the_restart_not_the_boilerplate(self, monkeypatch):
        responder, written = self._refuse(monkeypatch)
        assert written is not None, "nothing was answered — the SDK would have written the boilerplate"
        error = written.message.root.error
        assert error.message != SDK_TEXT
        assert "restarted at 12:22:44" in error.message
        assert "parameters you passed were not the problem" in error.message
        assert written.message.root.id == 7
        assert responder._completed, "the receive loop must not dispatch it a second time"

    def test_an_initialized_connection_is_untouched(self, monkeypatch):
        _responder, written = self._refuse(monkeypatch, initialized=True)
        assert written is None, "an initialized session answers nothing here; the handler does"


@pytest.mark.skipif(MCP_SDK_MAJOR < 2, reason="the 2.x runner gate")
class TestTheRefusalOnSdk2:
    """The runner raises the boilerplate inside the middleware chain; the
    guard's middleware is the link that rewrites it."""

    @staticmethod
    def _guard():
        mcp = FastMCP("guard-probe")
        assert install_uninitialized_request_guard(mcp)
        chain = lowlevel_server(mcp).middleware
        return [m for m in chain if getattr(m, mcp_compat._GUARDED, False)][-1]

    @staticmethod
    def _ctx(accepted: bool):
        connection = type("Conn", (), {"initialize_accepted": accepted})()
        session = type("Sess", (), {"_connection": connection})()
        return type("Ctx", (), {"session": session, "method": "tools/call"})()

    def test_it_reraises_with_the_restart_not_the_boilerplate(self, monkeypatch):
        from mcp.shared.exceptions import MCPError
        from mcp.types import INVALID_PARAMS

        monkeypatch.setenv(RESTART_MARKER_ENV, "2026-09-23T12:22:44-07:00")
        guard = self._guard()

        async def _sdk_refuses(_ctx):
            raise MCPError(code=INVALID_PARAMS, message=SDK_TEXT, data="")

        with pytest.raises(MCPError) as caught:
            anyio.run(guard, self._ctx(accepted=False), _sdk_refuses)
        assert caught.value.error.message != SDK_TEXT
        assert "restarted at 12:22:44" in caught.value.error.message
        assert caught.value.error.code == INVALID_PARAMS

    def test_the_same_text_on_an_initialized_connection_passes_through(self):
        from mcp.shared.exceptions import MCPError
        from mcp.types import INVALID_PARAMS

        guard = self._guard()

        async def _real_bad_params(_ctx):
            raise MCPError(code=INVALID_PARAMS, message=SDK_TEXT, data="")

        with pytest.raises(MCPError) as caught:
            anyio.run(guard, self._ctx(accepted=True), _real_bad_params)
        assert caught.value.error.message == SDK_TEXT

    def test_a_result_passes_through(self):
        guard = self._guard()

        async def _ok(_ctx):
            return {"ok": True}

        assert anyio.run(guard, self._ctx(accepted=False), _ok) == {"ok": True}


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
