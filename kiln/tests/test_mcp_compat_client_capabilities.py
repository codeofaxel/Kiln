"""``client_capabilities`` — the one read of "what did this host declare?".

The two SDK majors keep the session in different places: 1.x parks a request
context on the lowlevel server object, 2.x passes a ``ServerRequestContext``
to the handler and stores nothing on the server.  Before this accessor, the
stage read the 1.x attribute directly; on SDK 2 that raised, was swallowed,
and every host read as "declared nothing" — geometry never attached even
though the resource registered and the token hook installed.

These tests pin the contract with plain fakes; the end-to-end proof that a
declared host gets geometry through the real dispatch lives in
``test_local_stage.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

from kiln.mcp_compat import client_capabilities


def _ctx_with(caps: object) -> SimpleNamespace:
    return SimpleNamespace(
        session=SimpleNamespace(client_params=SimpleNamespace(capabilities=caps))
    )


def test_prefers_the_ctx_it_was_handed():
    """With a ctx carrying a session, the server object is never consulted —
    the fake here has no lowlevel server at all, so reaching for one would
    show up as a None."""
    caps = object()
    assert client_capabilities(SimpleNamespace(), _ctx_with(caps)) is caps


def test_falls_back_to_the_1x_server_attribute():
    """No ctx (the 1.x dispatch shape) still finds the session where 1.x
    keeps it, via ``lowlevel_server``'s ``_mcp_server`` fallback."""
    caps = object()
    mcp = SimpleNamespace(
        _mcp_server=SimpleNamespace(
            request_context=SimpleNamespace(
                session=SimpleNamespace(
                    client_params=SimpleNamespace(capabilities=caps)
                )
            )
        )
    )
    assert client_capabilities(mcp, None) is caps


def test_no_session_anywhere_is_none_not_a_crash():
    """"No session" is a legitimate answer — before the first request, or on
    a server driven outside a request. It must read as None, never raise."""
    bare = SimpleNamespace(_mcp_server=SimpleNamespace())  # no request_context
    assert client_capabilities(bare) is None
    assert client_capabilities(bare, SimpleNamespace(session=None)) is None
    assert client_capabilities(SimpleNamespace()) is None
