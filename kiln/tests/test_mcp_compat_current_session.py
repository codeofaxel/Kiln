"""``current_session`` — the one read of "who do I notify?".

Sibling of ``client_capabilities``: same object, different question, and
the same disagreement between SDK majors about where that object lives.
1.x parks a request context on the lowlevel server; 2.x removed that
attribute outright and hands a ``ServerRequestContext`` to the handler.

Before this accessor, ``local_stage._announce_tool_list_changed`` read
the 1.x attribute directly.  On SDK 2 that raised ``AttributeError``, the
caller's ``except`` turned it into "no session", and the tool-list
notification was silently never sent — on every make, with nothing
saying so.  Since geometry stopped riding the result that notification is
what makes the fetch verb callable by a host that validates tool names,
so the panel would have failed to load every time.

``capture_request_context`` is the other half: a ``FunctionResource``
function is handed no ctx by EITHER major, so on 2.x a resource read had
no route to a session at all until the one handler was wrapped.

These tests pin the contract with plain fakes.  The end-to-end proof that
a real ``resources/read`` on each major reaches a real session lives in
``test_local_stage.py::TestTheHostIsToldTheVerbArrived``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiln import mcp_compat
from kiln.mcp_compat import capture_request_context, current_session


def _ctx_with_session(session: object) -> SimpleNamespace:
    return SimpleNamespace(session=session)


class TestCurrentSession:
    def test_prefers_the_ctx_it_was_handed(self):
        """The 2.x shape: the ctx carries the session and the server object
        is never consulted — the fake here has no lowlevel server, so
        reaching for one would surface as None."""
        session = object()
        assert (
            current_session(SimpleNamespace(), _ctx_with_session(session)) is session
        )

    def test_falls_back_to_the_1x_server_attribute(self):
        session = object()
        mcp = SimpleNamespace(
            _mcp_server=SimpleNamespace(
                request_context=SimpleNamespace(session=session)
            )
        )
        assert current_session(mcp) is session

    def test_no_session_anywhere_is_none_not_a_raise(self):
        """Diagnostics read resources with no request in flight, and the
        REST proxy runs tools with no host to notify.  Both are ordinary."""
        assert current_session(SimpleNamespace()) is None

    def test_a_ctx_without_a_session_falls_through(self):
        """A ctx is not proof of a session — an SDK that hands over a
        context object with nothing on it must not shadow the fallback."""
        session = object()
        mcp = SimpleNamespace(
            _mcp_server=SimpleNamespace(
                request_context=SimpleNamespace(session=session)
            )
        )
        assert current_session(mcp, SimpleNamespace()) is session

    def test_the_ambient_ctx_is_used_when_no_ctx_is_passed(self, monkeypatch):
        """What ``capture_request_context`` exists to provide: code running
        inside a request that the SDK hands no ctx to."""
        session = object()
        token = mcp_compat._AMBIENT_CTX.set(_ctx_with_session(session))
        try:
            assert current_session(SimpleNamespace()) is session
        finally:
            mcp_compat._AMBIENT_CTX.reset(token)

    def test_a_passed_ctx_outranks_the_ambient_one(self):
        """The handler's own ctx is the truth for its own request; the
        ambient is a fallback, never an override."""
        ambient, handed = object(), object()
        token = mcp_compat._AMBIENT_CTX.set(_ctx_with_session(ambient))
        try:
            got = current_session(SimpleNamespace(), _ctx_with_session(handed))
            assert got is handed
        finally:
            mcp_compat._AMBIENT_CTX.reset(token)

    def test_the_ambient_does_not_leak_past_its_request(self):
        """Set and reset are paired inside the wrapper; nothing may be left
        behind for the next caller to read as their own session."""
        assert mcp_compat._AMBIENT_CTX.get() is None


class TestCaptureRequestContext:
    """The 2.x-only wrapper that restores an ambient request context."""

    def test_is_a_noop_on_sdk_1(self, monkeypatch):
        """1.x's dispatcher already sets an equivalent contextvar, so
        wrapping would be a second opinion about the same thing."""
        monkeypatch.setattr(mcp_compat, "MCP_SDK_MAJOR", 1)
        assert capture_request_context(SimpleNamespace(), "resources/read") is False

    def test_no_handler_is_false_not_a_raise(self, monkeypatch):
        monkeypatch.setattr(mcp_compat, "MCP_SDK_MAJOR", 2)
        mcp = SimpleNamespace(
            _lowlevel_server=SimpleNamespace(get_request_handler=lambda _m: None)
        )
        assert capture_request_context(mcp, "resources/read") is False

    def test_it_wraps_once_and_makes_the_ctx_ambient(self, monkeypatch):
        """The whole point: a callee handed nothing can still find the
        session, and a second install does not nest a second wrapper."""
        import anyio

        monkeypatch.setattr(mcp_compat, "MCP_SDK_MAJOR", 2)
        seen: list = []
        registered: dict = {}

        async def _handler(_ctx, _params):
            seen.append(current_session(SimpleNamespace()))
            return "ok"

        entry = SimpleNamespace(handler=_handler, params_type=object)

        def _add(method, params_type, handler):
            registered[method] = SimpleNamespace(
                handler=handler, params_type=params_type
            )

        server = SimpleNamespace(
            get_request_handler=lambda m: registered.get(m, entry),
            add_request_handler=_add,
        )
        mcp = SimpleNamespace(_lowlevel_server=server)

        assert capture_request_context(mcp, "resources/read") is True
        # Idempotent: the same wire installed twice is one wire.
        assert capture_request_context(mcp, "resources/read") is False

        session = object()
        wrapped = registered["resources/read"].handler
        ctx = _ctx_with_session(session)
        assert anyio.run(wrapped, ctx, None) == "ok"

        assert seen == [session], "the handler's ctx never became ambient"
        assert mcp_compat._AMBIENT_CTX.get() is None, "ambient outlived its request"

    def test_the_ambient_is_cleared_even_when_the_handler_raises(self, monkeypatch):
        """A failed read must not leave its session visible to the next one."""
        import anyio

        monkeypatch.setattr(mcp_compat, "MCP_SDK_MAJOR", 2)
        registered: dict = {}

        async def _boom(_ctx, _params):
            raise RuntimeError("read failed")

        entry = SimpleNamespace(handler=_boom, params_type=object)
        server = SimpleNamespace(
            get_request_handler=lambda m: registered.get(m, entry),
            add_request_handler=lambda m, p, h: registered.__setitem__(
                m, SimpleNamespace(handler=h, params_type=p)
            ),
        )
        mcp = SimpleNamespace(_lowlevel_server=server)
        capture_request_context(mcp, "resources/read")

        wrapped = registered["resources/read"].handler
        with pytest.raises(RuntimeError, match="read failed"):
            anyio.run(wrapped, _ctx_with_session(object()), None)
        assert mcp_compat._AMBIENT_CTX.get() is None
