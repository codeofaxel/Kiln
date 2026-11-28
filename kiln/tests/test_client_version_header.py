"""Every request to Kiln's servers names this Kiln's version.

The servers shape some answers by the release that asks (a refusal's words,
a minimum-version floor), so a request that names no version is answered as
if from no release at all.  The one place a request's headers get the
version is :func:`kiln.api_device.client_headers`.  These pin:

* the walk: every way out in :data:`kiln.outbound_guard.SENDERS` (the
  roster of modules that send to Kiln's servers, itself pinned against the
  source) builds each request's headers through ``client_headers`` -- an
  ``httpx`` / ``requests`` call's ``headers``, a ``urllib`` request's
  ``headers``, a relay socket's ``additional_headers``, and no ``urlopen`` of
  a bare address, which could carry none;
* the walk can fail: a sender planted with a bare header dict is caught;
* the wire: doors that send a request with nothing else to vouch for it are
  called against a recording transport and the header is on the request.
"""

from __future__ import annotations

import ast
import contextlib
from pathlib import Path

import pytest

import kiln
from kiln import outbound_guard
from kiln.api_device import CLIENT_VERSION_HEADER, client_headers

_SRC = Path(kiln.__file__).resolve().parent.parent

#: The calls that send, by the last part of their dotted name.
_HTTP_SENDS = {
    ("httpx", m) for m in ("get", "post", "put", "patch", "delete", "request", "stream")
} | {("requests", m) for m in ("get", "post", "put", "patch", "delete", "request")}
_SOCKET_SENDS = {("websockets", "connect"), ("websocket", "create_connection")}
_URLOPEN = {("urllib", "request", "urlopen"), ("request", "urlopen"), ("urlopen",)}


def _dotted(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return tuple(reversed(parts))


def _is_request(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _dotted(node.func)[-1:] == ("Request",)


class _Walk:
    """Whether each request one module builds carries ``client_headers``."""

    def __init__(self, tree: ast.Module) -> None:
        self.functions = {
            n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self._returns_headers: dict[str, bool] = {}

    def _bindings(self, fn: ast.AST, name: str) -> list[ast.AST]:
        out: list[ast.AST] = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                out.append(node.value)
            elif isinstance(node, ast.Call) and _dotted(node.func) == (name, "update") and node.args:
                out.append(node.args[0])
        return out

    def carries(self, fn: ast.AST, expr: ast.AST | None) -> bool:
        if expr is None:
            return False
        if isinstance(expr, ast.Call):
            dotted = _dotted(expr.func)
            if dotted[-1:] == ("client_headers",):
                return True
            local = dotted[-1] if dotted and (len(dotted) == 1 or dotted[0] in ("self", "cls")) else ""
            return bool(local) and local in self.functions and self._local_returns_headers(local)
        if isinstance(expr, ast.Name):
            return any(self.carries(fn, value) for value in self._bindings(fn, expr.id))
        if isinstance(expr, ast.IfExp):
            return self.carries(fn, expr.body) and self.carries(fn, expr.orelse)
        if isinstance(expr, ast.BoolOp):
            return any(self.carries(fn, v) for v in expr.values)
        if isinstance(expr, ast.Dict):
            return any(k is None and self.carries(fn, v) for k, v in zip(expr.keys, expr.values, strict=True))
        return False

    def _local_returns_headers(self, name: str) -> bool:
        if name not in self._returns_headers:
            self._returns_headers[name] = False  # a recursion reads as no
            fn = self.functions[name]
            self._returns_headers[name] = any(
                isinstance(n, ast.Return) and self.carries(fn, n.value) for n in ast.walk(fn)
            )
        return self._returns_headers[name]

    def findings(self) -> list[str]:
        out: list[str] = []
        for fn in self.functions.values():
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                dotted = _dotted(node.func)
                where = f"{fn.name} line {node.lineno}"
                if dotted[-2:] in _HTTP_SENDS or dotted in _HTTP_SENDS:
                    sent = next((k.value for k in node.keywords if k.arg == "headers"), None)
                    if not self.carries(fn, sent):
                        out.append(f"{where}: {'.'.join(dotted)} without client_headers")
                elif dotted[-2:] in _SOCKET_SENDS:
                    sent = next((k.value for k in node.keywords if k.arg in ("additional_headers", "header")), None)
                    if not self.carries(fn, sent):
                        out.append(f"{where}: {'.'.join(dotted)} without client_headers")
                elif _is_request(node):
                    sent = next((k.value for k in node.keywords if k.arg == "headers"), None)
                    if sent is None and len(node.args) >= 3:
                        sent = node.args[2]
                    if not self.carries(fn, sent):
                        out.append(f"{where}: a Request without client_headers")
                elif dotted in _URLOPEN:
                    arg = node.args[0] if node.args else None
                    built = _is_request(arg) or (
                        isinstance(arg, ast.Name) and any(_is_request(v) for v in self._bindings(fn, arg.id))
                    )
                    if not built:
                        out.append(f"{where}: urlopen of a bare address, which carries no headers")
        return out


def _senders_findings(sources: dict[str, str]) -> dict[str, list[str]]:
    return {path: found for path, text in sources.items() if (found := _Walk(ast.parse(text)).findings())}


def test_every_request_to_kilns_servers_names_this_kilns_version():
    sources = {path: (_SRC / path).read_text(encoding="utf-8") for path in outbound_guard.SENDERS}
    assert sources, "outbound_guard.SENDERS names no sender"
    assert _senders_findings(sources) == {}


def test_a_sender_with_a_bare_header_dict_is_caught():
    planted = {
        "kiln/planted.py": (
            "import httpx\nimport urllib.request\n\n\n"
            "def send(url, bearer):\n"
            "    return httpx.post(url, headers={'Authorization': f'Bearer {bearer}'})\n\n\n"
            "def old_send(url):\n"
            "    return urllib.request.urlopen(url)\n"
        ),
    }
    found = _senders_findings(planted)["kiln/planted.py"]
    assert any("send line 6" in f for f in found)
    assert any("urlopen of a bare address" in f for f in found)


def test_client_headers_names_the_running_kiln_and_keeps_the_rest():
    out = client_headers({"Authorization": "Bearer tok"})
    assert out == {"Authorization": "Bearer tok", CLIENT_VERSION_HEADER: kiln.__version__}


# ---------------------------------------------------------------------------
# on the wire
# ---------------------------------------------------------------------------


class _Stop(Exception):
    """Raised by the recording transport once it has seen the request."""


@pytest.fixture
def wire(monkeypatch):
    """Record the headers of every request and stop it there."""
    import urllib.request

    import httpx
    import requests

    seen: list[dict[str, str]] = []

    def _record(headers):
        seen.append({str(k).lower(): str(v) for k, v in dict(headers or {}).items()})
        raise _Stop

    for module, name in ((httpx, "get"), (httpx, "post"), (httpx, "request"),
                         (requests, "get"), (requests, "post"), (requests, "request")):
        monkeypatch.setattr(module, name, lambda *a, headers=None, **k: _record(headers))
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, *a, **k: _record(req.header_items()))
    return seen


def _sends_the_version(seen: list[dict[str, str]]) -> bool:
    return bool(seen) and all(h.get(CLIENT_VERSION_HEADER.lower()) == kiln.__version__ for h in seen)


def test_a_file_sent_up_for_a_tool(wire, monkeypatch, tmp_path):
    from kiln import served_makes

    monkeypatch.setattr(served_makes, "_bearer", lambda: "tok")
    part = tmp_path / "part.stl"
    part.write_bytes(b"solid x\nendsolid x\n")
    with contextlib.suppress(Exception):
        served_makes._upload_file(part)
    assert _sends_the_version(wire)


def test_a_spend_caps_request(wire):
    from kiln.plugins import spend_cap_tools

    with contextlib.suppress(Exception):
        spend_cap_tools._call("tok", "GET", "/api/billing/spend-caps")
    assert _sends_the_version(wire)


def test_a_print_approval_request(wire, monkeypatch):
    from kiln import bridge_client

    monkeypatch.setattr(bridge_client, "_machine_id", lambda: "machine-1")
    with contextlib.suppress(Exception):
        bridge_client._account_call("GET", "/delegations", "tok")
    assert _sends_the_version(wire)


def test_a_sign_in_command_request(wire):
    from kiln.cli import auth_commands

    with contextlib.suppress(Exception):
        auth_commands._http_get("/api/auth/whoami", bearer="tok")
    assert _sends_the_version(wire)


def test_a_sign_in_renewal(wire):
    from kiln import auth_session

    with contextlib.suppress(Exception):
        auth_session._post_refresh("refresh-token")
    assert _sends_the_version(wire)


def test_a_terms_request(wire):
    from kiln import terms

    with contextlib.suppress(Exception):
        terms._server_request("/api/terms/status", "GET", "tok")
    assert _sends_the_version(wire)


def test_a_stage_document_fetch(wire):
    from kiln import stage_cache

    with contextlib.suppress(Exception):
        stage_cache._fetch("stage", None)
    assert _sends_the_version(wire)

