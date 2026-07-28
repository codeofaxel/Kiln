"""In-chat sign-in tools (public Kiln plugin).

The device-code sign-in flow as two MCP tools, so an agent can sign a user
in without dropping to a terminal.  They run LOCALLY (the poll writes the
session token to the caller's own ``~/.kiln``), which is why they live in
public Kiln rather than a hosted proxy.  Thin wrappers over the already-
public ``kiln.cli.auth_commands`` helpers.
"""

from __future__ import annotations

import json

from kiln.mcp_compat import FastMCP

import kiln.plugins.auth_tools as at


def _tools():
    m = FastMCP("t")
    at.register(m)
    return {t.name: t.fn for t in m._tool_manager.list_tools()}


def test_registers_both_tools():
    fns = _tools()
    assert "kiln_signin" in fns and "kiln_signin_poll" in fns


def test_poll_requires_device_code():
    res = _tools()["kiln_signin_poll"](device_code="")
    assert res["success"] is False
    assert res["error"]["code"] == "INVALID_INPUT"


def test_signin_start_returns_verification_uri(monkeypatch):
    import kiln.cli.auth_commands as ac

    monkeypatch.setattr(ac, "_http_post", lambda path, body=None, **k: {
        "verification_uri": "https://app.kiln3d.com/device?code=KLN-AAAA-BBBB",
        "user_code": "KLN-AAAA-BBBB",
        "device_code": "secret-dc",
        "interval": 2,
        "expires_in": 900,
    })
    res = _tools()["kiln_signin"]()
    assert res["success"] is True
    assert res["verification_uri"].startswith("https://")
    assert res["device_code"] == "secret-dc"


def test_signin_start_network_failure_is_error(monkeypatch):
    import kiln.cli.auth_commands as ac

    def boom(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr(ac, "_http_post", boom)
    res = _tools()["kiln_signin"]()
    assert res["success"] is False
    assert res["error"]["code"] == "SIGNIN_START_FAILED"


def test_poll_pending(monkeypatch):
    import kiln.cli.auth_commands as ac

    monkeypatch.setattr(ac, "_http_post", lambda p, b=None, **k: {"status": "pending"})
    res = _tools()["kiln_signin_poll"](device_code="secret-dc")
    assert res["status"] == "pending"


def test_poll_success_writes_tokens_locally(monkeypatch, tmp_path):
    import kiln.cli.auth_commands as ac

    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.setattr(ac, "_http_post", lambda p, b=None, **k: {
        "status": "success",
        "access_token": "tok-123",
        "refresh_token": "r-123",
        "email": "a@b.com",
        "tier": "free",
        "has_entitlement": False,
    })
    res = _tools()["kiln_signin_poll"](device_code="secret-dc")
    assert res["status"] == "success"
    assert res["email"] == "a@b.com"
    token_file = tmp_path / ".kiln" / "auth_tokens.json"
    assert token_file.exists()
    written = json.loads(token_file.read_text())
    assert written["access_token"] == "tok-123"


def test_poll_success_without_token_is_error(monkeypatch, tmp_path):
    import kiln.cli.auth_commands as ac

    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.setattr(ac, "_http_post", lambda p, b=None, **k: {"status": "success"})
    res = _tools()["kiln_signin_poll"](device_code="secret-dc")
    assert res["success"] is False
    assert res["error"]["code"] == "TOKEN_RESPONSE_INCOMPLETE"


def test_get_started_account_block_signed_out(monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    import kiln.server as srv

    gs = next(
        (t.fn for t in srv.mcp._tool_manager.list_tools() if t.name == "get_started"),
        None,
    )
    assert gs is not None
    out = gs()
    assert out["account"]["signed_in"] is False
    assert out["account"]["tool"] == "kiln_signin"


def test_get_started_account_block_signed_in(monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    kdir = tmp_path / ".kiln"
    kdir.mkdir(parents=True)
    (kdir / "auth_tokens.json").write_text(
        json.dumps({"access_token": "tok", "email": "signed@in.com"})
    )
    import kiln.server as srv

    gs = next(t.fn for t in srv.mcp._tool_manager.list_tools() if t.name == "get_started")
    out = gs()
    assert out["account"]["signed_in"] is True
    assert out["account"]["email"] == "signed@in.com"
