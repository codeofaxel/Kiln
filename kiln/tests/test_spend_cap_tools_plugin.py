"""Spend-cap tools (public Kiln plugin).

Five tools that call the Kiln API as the person signed in on this machine.
What is pinned here is what a plain install must get right on its own: the
tools exist with no private package installed, they call as the signed-in
SESSION (never a license key, which names a plan and not a person), and a
refusal from the servers reaches the person with every field it carried.
"""

from __future__ import annotations

import sys

import pytest

from kiln.mcp_compat import FastMCP

import kiln.auth_session as auth_session
import kiln.plugins.spend_cap_tools as sc

TOOLS = (
    "kiln_spend_caps_show",
    "kiln_spend_caps_request_change",
    "kiln_spend_caps_confirm_change",
    "kiln_spend_caps_request_order_approval",
    "kiln_spend_caps_confirm_order_approval",
)


def _tools():
    m = FastMCP("t")
    sc.register(m)
    return {t.name: t.fn for t in m._tool_manager.list_tools()}


class _Resp:
    def __init__(self, body, status=200, text=""):
        self._body, self.status_code, self.text = body, status, text

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


@pytest.fixture
def wire(monkeypatch):
    """Every request these tools make, and what the servers answer."""
    import httpx

    sent: list[dict] = []
    answers: list[_Resp] = []

    def request(method, url, **kw):
        sent.append({"method": method, "url": url, **kw})
        return answers.pop(0) if answers else _Resp({"success": True})

    monkeypatch.setattr(httpx, "request", request)
    monkeypatch.setenv("KILN_API_URL", "https://api.example.test")
    return sent, answers


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setattr(
        auth_session,
        "resolve_session_bearer",
        lambda *a, **k: auth_session.SessionBearer(token="session-jwt", state="live"),
    )


def test_the_five_tools_register_without_the_private_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    assert set(TOOLS) <= set(_tools())


def test_the_server_registers_them():
    import kiln.server as server

    server._ensure_internal_tool_plugins_registered()
    names = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert set(TOOLS) <= names


@pytest.mark.parametrize("tool", TOOLS)
def test_signed_out_says_how_to_sign_in_and_calls_nothing(tool, wire, monkeypatch):
    sent, _ = wire
    monkeypatch.setattr(
        auth_session,
        "resolve_session_bearer",
        lambda *a, **k: auth_session.SessionBearer(
            token="", state="needs_signin", detail="Your Kiln session ended."
        ),
    )
    kwargs = {
        "kiln_spend_caps_request_change": {"monthly_dollars": 3000},
        "kiln_spend_caps_confirm_change": {"challenge_id": "c", "totp_code": "123456"},
        "kiln_spend_caps_request_order_approval": {"order_id": "o", "max_dollars": 10},
        "kiln_spend_caps_confirm_order_approval": {"challenge_id": "c", "totp_code": "123456"},
    }.get(tool, {})
    answer = _tools()[tool](**kwargs)
    assert answer["code"] == "SIGNIN_REQUIRED" and answer["status"] == "error"
    assert "kiln_signin" in answer["error"] and "Your Kiln session ended." in answer["error"]
    assert sent == []


def test_it_calls_as_the_session_even_when_a_license_key_is_set(wire, signed_in, monkeypatch):
    """The account routes act on a person.  A license key in the environment
    wins every other hosted call on this machine; it must not win here."""
    sent, answers = wire
    monkeypatch.setenv("KILN_LICENSE_KEY", "kiln_pro_license")
    answers.append(_Resp({
        "success": True,
        "caps": {"per_order_cents": 50000, "monthly_cents": 200000},
        "defaults": {"per_order_cents": 50000},
        "agent_flow_enabled": True,
    }))
    answer = _tools()["kiln_spend_caps_show"]()
    assert answer["status"] == "success"
    assert answer["summary"] == "per-order $500 / monthly $2,000"
    assert "next_step" not in answer
    (call,) = sent
    assert call["method"] == "GET"
    assert call["url"] == "https://api.example.test/api/billing/spend-caps"
    assert call["headers"]["Authorization"] == "Bearer session-jwt"


def test_show_points_at_the_switch_when_chat_changes_are_off(wire, signed_in):
    _, answers = wire
    answers.append(_Resp({"success": True, "caps": {}, "agent_flow_enabled": False}))
    answer = _tools()["kiln_spend_caps_show"]()
    assert answer["agent_flow_enabled"] is False
    assert answer["next_step"]["enable_url"].startswith("https://app.kiln3d.com/")


def test_a_change_is_two_calls_and_carries_what_was_asked(wire, signed_in):
    sent, answers = wire
    answers.append(_Resp({
        "success": True, "challenge_id": "ch_1",
        "current_caps": {"monthly_dollars": 2000},
        "requested_caps": {"monthly_dollars": 3000},
        "expires_at": "soon",
    }))
    answers.append(_Resp({"success": True, "new_caps": {"per_order_dollars": 500, "monthly_dollars": 3000}, "change_log_id": 7}))
    tools = _tools()
    first = tools["kiln_spend_caps_request_change"](monthly_dollars=3000, reason="x" * 500)
    assert first["challenge_id"] == "ch_1" and "confirm_change" in first["instructions"]
    second = tools["kiln_spend_caps_confirm_change"](challenge_id=" ch_1 ", totp_code=" 123456 ")
    assert second["summary"] == "Caps updated: per-order $500 / monthly $3,000"
    ask, confirm = sent
    assert ask["url"].endswith("/api/billing/spend-caps/request-change")
    assert ask["json"] == {"surface": "mcp", "monthly_dollars": 3000.0, "reason": "x" * 200}
    assert confirm["url"].endswith("/api/billing/spend-caps/confirm-change")
    assert confirm["json"] == {"challenge_id": "ch_1", "totp_code": "123456", "surface": "mcp"}


def test_a_refusal_keeps_every_field_the_servers_sent(wire, signed_in):
    _, answers = wire
    answers.append(_Resp({
        "success": False, "error": "opt_in_required",
        "message": "Turn on chat changes first.",
        "enable_url": "https://app.kiln3d.com/settings/billing/spend-caps",
    }, status=403))
    answers.append(_Resp({"success": False, "error": "rate_limited", "retry_after_seconds": 3600}, status=429))
    tools = _tools()
    off = tools["kiln_spend_caps_request_change"](per_order_dollars=900)
    assert off["code"] == "OPT_IN_REQUIRED"
    assert "enable_url=https://app.kiln3d.com/settings/billing/spend-caps" in off["error"]
    limited = tools["kiln_spend_caps_confirm_change"](challenge_id="c", totp_code="000000")
    assert limited["code"] == "RATE_LIMITED" and "retry_after_seconds=3600" in limited["error"]


def test_one_order_approval_round_trip(wire, signed_in):
    sent, answers = wire
    answers.append(_Resp({"success": True, "challenge_id": "ch_2", "order_summary": {"order_id": "ord_9"}}))
    answers.append(_Resp({"success": True, "approval_token": "tok_1", "order_id": "ord_9", "max_dollars": 640.0}))
    tools = _tools()
    first = tools["kiln_spend_caps_request_order_approval"](order_id="ord_9", max_dollars=640)
    assert first["challenge_id"] == "ch_2"
    second = tools["kiln_spend_caps_confirm_order_approval"](challenge_id="ch_2", totp_code="123456")
    assert second["approval_token"] == "tok_1" and "tok_1" in second["next_step"]
    assert sent[0]["json"] == {"order_id": "ord_9", "max_dollars": 640.0, "surface": "mcp"}
    assert sent[1]["url"].endswith("/api/billing/spend-caps/confirm-order-approval")


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("kiln_spend_caps_request_change", {}),
        ("kiln_spend_caps_confirm_change", {"challenge_id": "", "totp_code": "1"}),
        ("kiln_spend_caps_request_order_approval", {"order_id": "o", "max_dollars": 0}),
        ("kiln_spend_caps_request_order_approval", {"order_id": "o", "max_dollars": "lots"}),
        ("kiln_spend_caps_confirm_order_approval", {"challenge_id": "c", "totp_code": " "}),
    ],
)
def test_a_call_missing_what_it_needs_is_refused_before_any_request(tool, kwargs, wire, signed_in):
    sent, _ = wire
    assert _tools()[tool](**kwargs)["code"] == "INVALID_INPUT"
    assert sent == []


def test_servers_unreachable_or_not_json_is_said_not_raised(wire, signed_in, monkeypatch):
    import httpx

    _, answers = wire
    answers.append(_Resp(None, status=502, text="<html>bad gateway</html>"))
    garbled = _tools()["kiln_spend_caps_show"]()
    assert garbled["status"] == "error" and garbled["code"] == "NON_JSON_RESPONSE"

    def down(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "request", down)
    answer = _tools()["kiln_spend_caps_show"]()
    assert answer["status"] == "error" and "could not reach" in answer["error"]
