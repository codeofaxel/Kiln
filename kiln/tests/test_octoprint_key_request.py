"""Kiln asks OctoPrint for a key, and the person clicks Allow.

The fake OctoPrint below answers the way OctoPrint's Application Keys plugin
does: a probe answers 204, a request answers 201 with a request token (and,
from OctoPrint 1.8, an approval page), and asking about the request answers
202 until the person decides, then 200 with the key or 404 when denied.
Each test walks a door the way a person or an agent meets it.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from kiln import octoprint_appkeys
from kiln.printer_backends import REQUEST_OFFERS, backend_for

KEY = "kiln-app-key-0123456789"
TOKEN = "app-token-abc"


class FakeOctoPrint:
    def __init__(self) -> None:
        self.supported = True
        self.has_approval_page = True
        self.decision: bool | None = None
        self.requests = 0
        self.api_status = 200
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):  # quiet
                pass

            def _send(self, code: int, body: dict | None = None, headers: dict | None = None) -> None:
                self.send_response(code)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                payload = json.dumps(body).encode() if body is not None else b""
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):  # noqa: N802
                if self.path == "/plugin/appkeys/probe":
                    return self._send(204 if owner.supported else 404)
                if self.path == f"/plugin/appkeys/request/{TOKEN}":
                    if owner.decision is None:
                        return self._send(202, {"message": "Awaiting decision"})
                    if owner.decision:
                        return self._send(200, {"api_key": KEY})
                    return self._send(404)
                if self.path.startswith("/api/"):
                    return self._send(owner.api_status, {"error": "Forbidden"} if owner.api_status != 200 else {})
                return self._send(404)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/plugin/appkeys/request" and body.get("app") == "Kiln":
                    owner.requests += 1
                    reply = {"app_token": TOKEN}
                    if owner.has_approval_page:
                        # OctoPrint names this from the headers it saw; Kiln
                        # builds its own from the address it used instead.
                        reply["auth_dialog"] = f"http://proxied.example/plugin/appkeys/auth/{TOKEN}"
                    return self._send(201, reply, {"Location": f"/plugin/appkeys/request/{TOKEN}"})
                return self._send(400)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def octoprint(monkeypatch):
    monkeypatch.setattr(octoprint_appkeys, "POLL_INTERVAL_S", 0.02)
    monkeypatch.setattr(octoprint_appkeys, "_OPEN", {})
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    fake = FakeOctoPrint()
    yield fake
    fake.decision = False  # lets any background request end
    fake.close()


def _wait_until_decided(url: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        entry = octoprint_appkeys._OPEN.get(url)
        if entry is not None and entry.state != "pending":
            return
        time.sleep(0.02)
    raise AssertionError("the background request never saw the decision")


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def test_a_granted_request_hands_back_the_key(octoprint) -> None:
    request = octoprint_appkeys.start(octoprint.url)
    assert request.approve_url == f"{octoprint.url}/plugin/appkeys/auth/{TOKEN}"
    octoprint.decision = True
    assert octoprint_appkeys.wait_for_key(request) == KEY


def test_a_denied_request_hands_back_nothing(octoprint) -> None:
    request = octoprint_appkeys.start(octoprint.url)
    octoprint.decision = False
    assert octoprint_appkeys.wait_for_key(request) is None


def test_before_octoprint_1_8_the_person_is_sent_to_octoprint_itself(octoprint) -> None:
    octoprint.has_approval_page = False
    request = octoprint_appkeys.start(octoprint.url)
    assert request.approve_url is None
    assert "open OctoPrint in a browser" in octoprint_appkeys.how_to_approve(request)


def test_an_octoprint_without_the_plugin_is_not_asked(octoprint) -> None:
    octoprint.supported = False
    assert octoprint_appkeys.supported(octoprint.url) is False


# ---------------------------------------------------------------------------
# The agent's door: register_printer
# ---------------------------------------------------------------------------


@pytest.fixture
def server(monkeypatch, tmp_path: Path):
    from kiln import server as srv

    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: tmp_path / "config.yaml")
    yield srv
    if "octopi-test" in srv._get_registry():
        srv._get_registry().unregister("octopi-test")


def _register(srv, url: str) -> dict:
    return srv.register_printer(
        name="octopi-test", printer_type="octoprint", host=url, persist=False, verify_connection=False
    )


def test_register_printer_asks_octoprint_and_uses_the_key_once_allowed(octoprint, server) -> None:
    first = _register(server, octoprint.url)
    assert first["success"] is False
    assert first["error"]["code"] == "AWAITING_APPROVAL", first
    assert first["approve_url"] == f"{octoprint.url}/plugin/appkeys/auth/{TOKEN}"
    assert "click Allow" in first["error"]["message"]

    again = _register(server, octoprint.url)
    assert again["error"]["code"] == "AWAITING_APPROVAL"
    assert octoprint.requests == 1, "a second call must not start a second request"

    octoprint.decision = True
    _wait_until_decided(octoprint.url)
    done = _register(server, octoprint.url)
    assert done["success"] is True, done
    assert server._get_registry().get("octopi-test")._api_key == KEY


def test_a_denied_request_falls_back_to_copying_the_key(octoprint, server) -> None:
    assert _register(server, octoprint.url)["error"]["code"] == "AWAITING_APPROVAL"
    octoprint.decision = False
    _wait_until_decided(octoprint.url)
    refused = _register(server, octoprint.url)
    assert refused["error"]["code"] == "INVALID_ARGS"
    message = refused["error"]["message"]
    need = backend_for("octoprint").needs[0]
    assert need.where in message and "denied or ran out" in message, message


def test_an_octoprint_that_cannot_hand_out_keys_gets_the_old_answer(octoprint, server) -> None:
    octoprint.supported = False
    refused = _register(server, octoprint.url)
    assert refused["error"]["code"] == "INVALID_ARGS"
    assert octoprint.requests == 0


def test_the_hosted_server_never_starts_a_request(octoprint, monkeypatch) -> None:
    monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
    assert octoprint_appkeys.ask_in_background(octoprint.url)["state"] == "error"
    assert octoprint.requests == 0


# ---------------------------------------------------------------------------
# The terminal's doors: kiln setup and the bridge share this prompt
# ---------------------------------------------------------------------------


def test_the_terminal_shows_where_to_approve_and_keeps_the_key(octoprint, capsys) -> None:
    from kiln.cli.connection_prompt import ask_connection_needs

    octoprint.decision = True
    with patch("click.confirm", return_value=True):
        answers = ask_connection_needs("octoprint", discovered=True, host=octoprint.url)

    assert answers["api_key"] == KEY
    assert f"{octoprint.url}/plugin/appkeys/auth/{TOKEN}" in capsys.readouterr().out


def test_declining_the_offer_asks_for_the_key_by_hand(octoprint) -> None:
    from kiln.cli.connection_prompt import ask_connection_needs

    with patch("click.confirm", return_value=False), patch("click.prompt", return_value="typed-key"):
        answers = ask_connection_needs("octoprint", discovered=True, host=octoprint.url)

    assert answers["api_key"] == "typed-key"
    assert octoprint.requests == 0


# ---------------------------------------------------------------------------
# What every refusal says, and OctoPrint's own refusal
# ---------------------------------------------------------------------------


def test_every_sentence_about_the_key_offers_the_request() -> None:
    from kiln.printer_backends import needs_sentence, setup_summary

    need = backend_for("octoprint").needs[0]
    offer = REQUEST_OFFERS[need.request]
    assert offer in needs_sentence("octoprint", [need])
    assert offer in setup_summary()


def test_every_request_a_need_names_has_a_flow_and_an_offer() -> None:
    from kiln.printer_backends import PRINTER_BACKENDS

    named = {need.request for backend in PRINTER_BACKENDS for need in backend.needs if need.request}
    assert named == set(REQUEST_OFFERS), (named, set(REQUEST_OFFERS))


def test_octoprint_refusing_the_key_says_where_to_get_one(octoprint) -> None:
    from kiln.printers.base import PrinterError
    from kiln.printers.octoprint import OctoPrintAdapter

    octoprint.api_status = 403
    adapter = OctoPrintAdapter(host=octoprint.url, api_key="wrong", retries=1)
    with pytest.raises(PrinterError) as exc_info:
        adapter._request("GET", "/api/printer")

    message = str(exc_info.value)
    need = backend_for("octoprint").needs[0]
    assert need.where in message and REQUEST_OFFERS[need.request] in message, message


def test_kiln_setup_gets_a_found_octoprints_key_from_octoprint(octoprint, tmp_path: Path) -> None:
    """End to end: discovery found OctoPrint on its own port, setup asks it
    for a key at that port, the person allows it, and the key is saved."""
    from unittest.mock import MagicMock

    from click.testing import CliRunner

    from kiln.cli.main import cli
    from kiln.discovery import DiscoveredPrinter

    port = int(octoprint.url.rsplit(":", 1)[1])
    found = DiscoveredPrinter(host="127.0.0.1", port=port, printer_type="octoprint", name="OctoPi")
    octoprint.decision = True
    with (
        patch("kiln.terms.is_current", return_value=True),
        patch("kiln.cli.config.get_config_path", return_value=tmp_path / "config.yaml"),
        patch("kiln.cli.discovery.discover_printers", return_value=[found]),
        patch("kiln.cli.printer_model_prompt.prompt_for_printer_model", return_value=None),
        patch("kiln.cli.main.save_printer", return_value=tmp_path / "config.yaml") as save,
        patch("kiln.cli.main._make_adapter", return_value=MagicMock()),
    ):
        result = CliRunner().invoke(cli, ["setup"], input="1\noctopi\ny\n")

    assert save.called, result.output
    assert save.call_args.args[2] == f"127.0.0.1:{port}"
    assert save.call_args.kwargs["api_key"] == KEY, result.output
    assert f"/plugin/appkeys/auth/{TOKEN}" in result.output


def test_the_bridge_gets_a_found_octoprints_key_from_octoprint(octoprint) -> None:
    from kiln.cli.bridge_commands import _credential_prompts

    octoprint.decision = True
    with patch("click.confirm", return_value=True):
        assert _credential_prompts("octoprint", "", host=octoprint.url) == {"api_key": KEY}
