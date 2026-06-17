"""Tests for the MCP Terms-of-Use gate + the ``accept_terms`` tool.

The gate lives at the single dispatch chokepoint (``_call_tool_with_context``):
the first substantive MCP tool call by an un-accepted identity is blocked with a
one-time consent gate (raised so the lowlevel handler relays it).  Whitelisted
tools (orient / tier / health / accept) always pass.
"""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from kiln import server


# --- _terms_gate_blocks ----------------------------------------------------


def test_whitelisted_tools_never_blocked(monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: False)
    for name in ("get_started", "check_my_tier", "kiln_health", "accept_terms"):
        assert server._terms_gate_blocks(name) is False


def test_non_whitelisted_blocked_when_not_accepted(monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: False)
    assert server._terms_gate_blocks("printer_status") is True


def test_non_whitelisted_allowed_when_accepted(monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: True)
    assert server._terms_gate_blocks("printer_status") is False


def test_gate_fails_open_on_terms_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("kiln.terms.is_current", _boom)
    # An infrastructure error must never block a tool call.
    assert server._terms_gate_blocks("printer_status") is False


# --- the consent message ---------------------------------------------------


def test_consent_message_carries_notice_and_phrase():
    msg = server._terms_consent_message()
    assert server._TERMS_ACCEPT_PHRASE in msg
    assert "https://kiln3d.com/terms" in msg
    assert "Business" in msg  # the tier rule from the summary is present (real notice)
    # The anti-inference guardrail is the whole point of in-chat consent — pin it
    # so it can't be silently dropped from _terms_consent_message.
    assert "infer" in msg.lower()
    assert "on the user's behalf" in msg.lower()


# --- the wrapper raises the gate at the chokepoint -------------------------


def test_wrapper_raises_consent_when_blocked(monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: False)
    # A non-whitelisted tool: the gate raises BEFORE the real tool runs.
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(
            server.mcp._tool_manager.call_tool(
                "printer_status", {}, context=None, convert_result=True
            )
        )
    assert server._TERMS_ACCEPT_PHRASE in str(exc.value)


# --- the accept_terms tool core --------------------------------------------


def test_record_mcp_acceptance_records_with_verbatim(monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: False)
    rec = mock.MagicMock()
    monkeypatch.setattr("kiln.terms.record_acceptance", rec)
    out = server._record_mcp_acceptance("I accept the Kiln Terms")
    assert out["accepted"] is True
    rec.assert_called_once()
    assert rec.call_args.kwargs.get("method") == "mcp_in_chat"
    assert rec.call_args.kwargs.get("verbatim_text") == "I accept the Kiln Terms"


def test_record_mcp_acceptance_idempotent_when_current(monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: True)
    rec = mock.MagicMock()
    monkeypatch.setattr("kiln.terms.record_acceptance", rec)
    out = server._record_mcp_acceptance("whatever")
    assert out["accepted"] is True
    rec.assert_not_called()  # already accepted -> no re-record
