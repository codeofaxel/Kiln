"""Tests for the MCP Terms-of-Use gate (one-tap accept link).

The gate sits at the single dispatch chokepoint (``_call_tool_with_context``):
the first substantive MCP tool call by an un-accepted identity is blocked with a
one-time consent gate (raised so the lowlevel handler relays it).  There is NO
in-chat accept tool — the agent cannot accept on the user's behalf; only a human
action does (tap the account link, or run ``kiln accept-terms``).  Whitelisted
orient tools always pass.
"""

from __future__ import annotations

import asyncio
import time
from unittest import mock

import pytest

from kiln import server


@pytest.fixture(autouse=True)
def _reset_pending(monkeypatch):
    # Keep the in-process force-poll window from leaking between tests.
    monkeypatch.setattr(server, "_accept_link_pending_until", 0.0, raising=False)


# --- _terms_gate_blocks ----------------------------------------------------


def test_whitelisted_tools_never_blocked(monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: False)
    for name in ("get_started", "check_my_tier", "kiln_health"):
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
    assert server._terms_gate_blocks("printer_status") is False


def test_gate_force_polls_only_during_pending_window(monkeypatch):
    captured = {}

    def _is_current(*a, **k):
        captured["force_server"] = k.get("force_server")
        return False

    monkeypatch.setattr("kiln.terms.is_current", _is_current)

    monkeypatch.setattr(server, "_accept_link_pending_until", 0.0, raising=False)
    server._terms_gate_blocks("printer_status")
    assert captured["force_server"] is False

    monkeypatch.setattr(server, "_accept_link_pending_until", time.time() + 100, raising=False)
    server._terms_gate_blocks("printer_status")
    assert captured["force_server"] is True


# --- the consent message ---------------------------------------------------


def test_consent_message_account_offers_the_link(monkeypatch):
    monkeypatch.setattr(server, "_mint_accept_link", lambda: "https://kiln3d.com/accept/TOK123")
    msg = server._terms_consent_message()
    assert "https://kiln3d.com/accept/TOK123" in msg
    assert "tap" in msg.lower()
    assert "Business" in msg  # the 6-bullet summary is the notice
    assert "can't accept for you" in msg.lower()


def test_consent_message_account_sets_force_poll_window(monkeypatch):
    monkeypatch.setattr(server, "_mint_accept_link", lambda: "https://kiln3d.com/accept/X")
    server._terms_consent_message()
    assert server._accept_link_pending_until > time.time()


def test_consent_message_no_account_points_to_cli(monkeypatch):
    monkeypatch.setattr(server, "_mint_accept_link", lambda: None)
    msg = server._terms_consent_message()
    assert "kiln accept-terms" in msg
    assert "kiln3d.com" in msg
    assert "Business" in msg
    # no link minted -> no force-poll window opened
    assert server._accept_link_pending_until <= time.time()


# --- there is no self-serve accept (the agent cannot accept) ----------------


def test_no_accept_terms_tool():
    assert not hasattr(server, "accept_terms")
    assert not hasattr(server, "_record_mcp_acceptance")
    assert "accept_terms" not in getattr(server.mcp._tool_manager, "_tools", {})


# --- the wrapper raises the gate at the chokepoint -------------------------


def test_wrapper_raises_consent_when_blocked(monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: False)
    monkeypatch.setattr(server, "_mint_accept_link", lambda: None)  # no account -> CLI path
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(
            server.mcp._tool_manager.call_tool(
                "printer_status", {}, context=None, convert_result=True
            )
        )
    assert "kiln accept-terms" in str(exc.value)
