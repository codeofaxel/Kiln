"""On a signed-in machine the account is one more door a person's yes can
come through: the print is put to the account beside the screen's code,
any signed-in browser at kiln3d.com/monitor can answer it, and the next
start reads the answer.

The threats these pin: the agent must not be able to turn an account
read into a yes (only an answer the server gives, for these bytes on this
printer, reported back as a start the server accepted); a decline is a
no, but only for the ask this machine posted; nothing changes for a
machine that is not signed in, for a server that does not answer, or on
the hosted server; and the refusal names one fixed page, never a link
for this print.  The server is faked with ``responses``: these tests pin
the fields this side sends and reads, not what the server decides.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import pathlib
import time
import types
from unittest.mock import MagicMock, patch

import pytest
import requests
import responses

from kiln import (
    auth_session,
    bridge_client,
    consent_windows,
    preview_evidence,
    print_consent,
    print_signoff,
    screen_code,
    server,
)
from kiln.preview_gate import PreviewGate
from kiln.print_consent import (
    NOT_ASKED_CODE_SHOWN,
    NOT_ASKED_HOST_CANNOT,
    NOT_ASKED_PENDING_TAG,
    SOURCE_HOSTED_APPROVAL,
    SOURCE_HOSTED_DELEGATION,
    consent_for,
    grade_of,
    reset_consent,
    why_not_asked,
)

API = "https://api.account.test"
PENDING = f"{API}/api/print-authority/pending"
MAY_I = f"{API}/api/print-authority/may-i-print"
RECORD = f"{API}/api/print-authority/record-start"
MACHINE = "ab" * 16


def _jwt(exp: float) -> str:
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{seg({'alg': 'none'})}.{seg({'exp': exp})}.sig"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    monkeypatch.setenv("KILN_API_URL", API)
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path / "auth"))
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln_home"))
    monkeypatch.setattr(auth_session, "_last_network_failure_monotonic", None)
    preview_evidence._reset_for_tests()
    print_signoff._reset_for_tests()
    print_consent._reset_for_tests()
    consent_windows._reset_for_tests()
    screen_code._reset_for_tests()
    bridge_client._reset_asks_for_tests()
    import kiln.preview_gate as pg

    monkeypatch.setattr(pg, "_gate", PreviewGate())
    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: False)
    monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda name=None: name or "bench")
    monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: False)
    monkeypatch.setattr(consent_windows, "_fleet_tier_allows", lambda: False)
    monkeypatch.setattr(consent_windows, "_path", lambda: tmp_path / "consent_windows.json")
    monkeypatch.setattr(screen_code, "screen_available", lambda: True)
    monkeypatch.setattr(screen_code, "MIN_READ_S", 0.0)
    monkeypatch.setattr(screen_code, "_show_hook", lambda issued: True)
    import kiln.device

    monkeypatch.setattr(kiln.device, "get_device_fingerprint", lambda: MACHINE)
    yield
    screen_code._reset_for_tests()
    preview_evidence._reset_for_tests()
    print_signoff._reset_for_tests()
    print_consent._reset_for_tests()
    consent_windows._reset_for_tests()
    bridge_client._reset_asks_for_tests()


@pytest.fixture
def signed_in(tmp_path):
    """A ``kiln signin`` session on disk, live for an hour."""
    home = tmp_path / "auth" / ".kiln"
    home.mkdir(parents=True)
    (home / "auth_tokens.json").write_text(json.dumps({
        "access_token": _jwt(time.time() + 3600), "refresh_token": "rt", "email": "p@example.com", "auth_uid": "uid-1",
    }))
    return "Bearer " + json.loads((home / "auth_tokens.json").read_text())["access_token"]


@pytest.fixture
def observed(monkeypatch):
    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr(bridge_client, "_observe_in_background", lambda api, bearer, nonce: seen.append((api, bearer, nonce)))
    return seen


@pytest.fixture
def audits(monkeypatch):
    seen: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(server, "_audit", lambda tool, action, details=None: seen.append((tool, action, details or {})))
    return seen


@pytest.fixture
def model(tmp_path):
    path = tmp_path / "benchy.3mf"
    path.write_bytes(b"solid benchy " + str(tmp_path).encode())
    return path


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ask(file_name, printer_name="bench", tool="start_print"):
    """One start attempt, judged inside its own call the way the gate reads it."""

    async def run():
        token = await server._obtain_print_consent(
            tool, {"file_name": str(file_name), "printer_name": printer_name}, types.SimpleNamespace(),
        )
        try:
            return types.SimpleNamespace(
                why=why_not_asked(),
                consent=consent_for(file_name=str(file_name), printer_name=printer_name),
                text=server._no_yes_message(tool, str(file_name), printer_name),
            )
        finally:
            reset_consent(token)

    return asyncio.run(run())


def _may_i(allowed=False, authority=None, pending=None):
    responses.add(responses.GET, MAY_I, json={
        "success": True, "allowed": allowed, "authority": authority, "pending": pending,
    })


def _held(pending_id="pa_1", repeat=False, expires_at=None):
    responses.add(responses.POST, PENDING, json={"success": True, "pending": {
        "id": pending_id, "expires_at": expires_at or time.time() + 600, "page": "/monitor",
        "state": "waiting", "repeat": repeat,
    }})


def _calls(url: str) -> list:
    return [c for c in responses.calls if c.request.url.split("?", 1)[0] == url]


def _png(tmp_path: pathlib.Path, name: str, size=(64, 48), noise=False) -> pathlib.Path:
    from PIL import Image

    if noise:
        import os

        image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    else:
        image = Image.new("RGB", size, (200, 120, 40))
    path = tmp_path / name
    image.save(path, format="PNG")
    return path


# ---------------------------------------------------------------------------
# Rung 0: a yes the account already holds
# ---------------------------------------------------------------------------


class TestTheAccountsYes:
    @responses.activate
    def test_an_approval_is_the_yes_and_is_spent_once(self, signed_in, model, audits):
        _may_i(True, {"kind": "approval", "id": "ap_1", "grantor": "account:acct-1", "expires_at": "2026-09-24T12:00:00Z", "via": "web_button"})
        responses.add(responses.POST, RECORD, json={"success": True, "event": {"id": "ev_1"}, "repeat": False})
        r = _ask(model)
        assert r.consent is not None and r.consent.source == SOURCE_HOSTED_APPROVAL
        assert grade_of(r.consent.source) == "A" and r.consent.identity == "account:acct-1"
        assert r.consent.door == ""  # the account's yes shows nothing here: the token proves the preview
        assert len(_calls(RECORD)) == 1 and _calls(PENDING) == []
        sent = json.loads(_calls(RECORD)[0].request.body)
        assert sent == {"authority_id": "ap_1", "kind": "approval", "file_sha256": _sha(model), "printer_name": "bench"}
        assert _calls(MAY_I)[0].request.headers["Authorization"] == signed_in
        granted = [d for _, a, d in audits if a == "consent_granted"]
        assert granted and granted[0]["source"] == SOURCE_HOSTED_APPROVAL and granted[0]["identity"] == "account:acct-1"

    @responses.activate
    def test_the_read_names_the_printer_and_the_bytes(self, signed_in, model):
        _may_i()
        _held()
        _ask(model)
        query = requests.utils.urlparse(_calls(MAY_I)[0].request.url).query
        assert sorted(query.split("&")) == sorted(["printer_name=bench", f"file_hash={_sha(model)}"])

    @responses.activate
    def test_a_window_for_this_machine_is_a_yes_with_its_end(self, signed_in, model):
        until = time.time() + 7200
        _may_i(True, {"kind": "machine_window", "id": "dl_9", "grantor": "account:acct-1", "expires_at": until, "via": "web_button"})
        responses.add(responses.POST, RECORD, json={"success": True, "event": {"id": "ev_2"}, "repeat": False})
        r = _ask(model)
        assert r.consent is not None and r.consent.source == SOURCE_HOSTED_DELEGATION
        assert r.consent.window_id == "dl_9" and abs(r.consent.expires_at - until) < 1
        assert json.loads(_calls(RECORD)[0].request.body)["kind"] == "machine_window"

    @responses.activate
    def test_a_start_the_server_will_not_record_is_no_yes(self, signed_in, model):
        _may_i(True, {"kind": "approval", "id": "ap_1", "grantor": "account:acct-1", "expires_at": time.time() + 600})
        responses.add(responses.POST, RECORD, status=403, json={"error": "authority_mismatch", "message": "no"})
        _held()
        r = _ask(model)
        assert r.consent is None and r.why.startswith(NOT_ASKED_CODE_SHOWN)

    @responses.activate
    def test_an_unknown_kind_is_no_yes(self, signed_in, model):
        _may_i(True, {"kind": "delegation", "id": "dl_1", "grantor": "account:acct-1", "expires_at": time.time() + 600})
        _held()
        r = _ask(model)
        assert r.consent is None and _calls(RECORD) == []

    @responses.activate
    def test_a_decline_of_this_machines_ask_refuses_once_then_it_asks_again(self, signed_in, model, audits, observed):
        _may_i()
        _held("pa_7")
        first = _ask(model)
        assert first.why.endswith(NOT_ASKED_PENDING_TAG + "pa_7")
        responses.replace(responses.GET, MAY_I, json={"success": True, "allowed": False, "authority": None, "pending": {"id": "pa_7", "state": "declined"}})
        with pytest.raises(RuntimeError, match="declined"):
            _ask(model)
        assert any(a == "consent_refused" and d.get("door") == "account" for _, a, d in audits)
        # The no was for that ask.  The next start is asked again, not refused forever.
        responses.replace(responses.POST, PENDING, json={"success": True, "pending": {"id": "pa_8", "expires_at": time.time() + 600, "page": "/monitor", "state": "waiting", "repeat": False}})
        again = _ask(model)
        assert again.consent is None and again.why.endswith(NOT_ASKED_PENDING_TAG + "pa_8")

    @responses.activate
    def test_a_decline_this_process_never_asked_for_refuses_nothing(self, signed_in, model):
        _may_i(pending={"id": "pa_other", "state": "declined"})
        _held("pa_new")
        r = _ask(model)
        assert r.why.startswith(NOT_ASKED_CODE_SHOWN) and r.why.endswith("pa_new")


# ---------------------------------------------------------------------------
# Rung 3: the print is put to the account beside the code
# ---------------------------------------------------------------------------


class TestTheAsk:
    @responses.activate
    def test_the_ask_is_posted_once_while_it_is_live(self, signed_in, model, audits, observed):
        _may_i()
        _held("pa_1")
        r1 = _ask(model)
        r2 = _ask(model)
        assert len(_calls(PENDING)) == 1
        assert r1.why.endswith(NOT_ASKED_PENDING_TAG + "pa_1") and r2.why.endswith(NOT_ASKED_PENDING_TAG + "pa_1")
        posted = [d for _, a, d in audits if a == "consent_pending_posted"]
        assert len(posted) == 1 and posted[0]["pending"] == "pa_1" and posted[0]["picture_sent"] is False
        # The machine is shown to the relay once, with the ask's own nonce.
        body = json.loads(_calls(PENDING)[0].request.body)
        assert observed == [(API, signed_in.split(" ", 1)[1], body["observe_nonce"])]

    @responses.activate
    def test_the_ask_sends_exactly_the_contract_fields(self, signed_in, model):
        _may_i()
        _held()
        _ask(model)
        body = json.loads(_calls(PENDING)[0].request.body)
        assert set(body) == {
            "file_sha256", "file_name", "printer_name", "shown_pixels_sha", "shown_door",
            "picture_png_b64", "machine_fingerprint", "observe_nonce",
        }
        assert body["file_sha256"] == _sha(model) and body["file_name"] == "benchy.3mf"
        assert body["printer_name"] == "bench" and body["machine_fingerprint"] == MACHINE
        assert body["observe_nonce"]

    @responses.activate
    def test_the_picture_the_preview_recorded_rides_the_ask_hashed_as_sent(self, signed_in, model, tmp_path):
        still = _png(tmp_path, "iso.png")
        preview_evidence.record("png", str(model), renderer="stage_paint", shown_sha="abc", picture=str(still))
        _may_i()
        _held()
        _ask(model)
        body = json.loads(_calls(PENDING)[0].request.body)
        png = base64.b64decode(body["picture_png_b64"])
        assert png == still.read_bytes()
        assert body["shown_pixels_sha"] == hashlib.sha256(png).hexdigest() and body["shown_door"] == "png"

    @responses.activate
    def test_a_picture_too_large_is_shrunk_and_the_hash_is_of_what_is_sent(self, signed_in, model, tmp_path):
        still = _png(tmp_path, "big.png", size=(800, 600), noise=True)
        assert still.stat().st_size > bridge_client.PICTURE_MAX_BYTES
        preview_evidence.record("png", str(model), renderer="stage", shown_sha="abc", picture=str(still))
        _may_i()
        _held()
        _ask(model)
        body = json.loads(_calls(PENDING)[0].request.body)
        png = base64.b64decode(body["picture_png_b64"])
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) <= bridge_client.PICTURE_MAX_BYTES
        assert body["shown_pixels_sha"] == hashlib.sha256(png).hexdigest()

    @responses.activate
    def test_a_raw_render_is_not_sent_as_the_picture(self, signed_in, model, tmp_path):
        still = _png(tmp_path, "raw.png")
        preview_evidence.record("png", str(model), renderer="openscad", shown_sha="abc", picture=str(still))
        _may_i()
        _held()
        _ask(model)
        body = json.loads(_calls(PENDING)[0].request.body)
        assert body["picture_png_b64"] == "" and body["shown_pixels_sha"] == ""

    @responses.activate
    def test_an_ask_the_server_refuses_is_audited_and_names_no_page(self, signed_in, model, audits, observed):
        _may_i()
        responses.add(responses.POST, PENDING, status=400, json={"error": "picture_invalid", "message": "no picture"})
        r = _ask(model)
        assert NOT_ASKED_PENDING_TAG not in r.why and "kiln3d.com/monitor" not in r.text
        refused = [d for _, a, d in audits if a == "consent_pending_refused"]
        assert refused and refused[0]["reason"] == "picture_invalid"
        assert observed == []

    @responses.activate
    def test_the_ask_runs_where_no_code_can_be_shown(self, signed_in, model, monkeypatch):
        monkeypatch.setattr(screen_code, "screen_available", lambda: False)
        _may_i()
        _held("pa_3")
        r = _ask(model)
        assert r.why == NOT_ASKED_HOST_CANNOT + NOT_ASKED_PENDING_TAG + "pa_3"
        assert "kiln3d.com/monitor" in r.text and "no dialog is coming" in r.text

    @responses.activate
    def test_the_ask_runs_while_the_code_is_cooling_down(self, signed_in, model):
        screen_code._cooldown_until["bench"] = time.time() + 60
        _may_i()
        _held("pa_4")
        r = _ask(model)
        assert r.why.startswith(print_consent.NOT_ASKED_CODE_COOLDOWN) and r.why.endswith(NOT_ASKED_PENDING_TAG + "pa_4")
        assert "seconds" in r.text and "kiln3d.com/monitor" in r.text
        assert r.text.index("kiln3d.com/monitor") < r.text.index("kiln print benchy.3mf")


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------


class TestTheRefusal:
    @responses.activate
    def test_it_names_the_fixed_page_and_never_a_link_for_this_print(self, signed_in, model):
        _may_i()
        _held("pa_secret_id")
        r = _ask(model)
        assert "kiln3d.com/monitor" in r.text
        assert "pa_secret_id" not in r.text and "http" not in r.text and "kiln3d.com/monitor/" not in r.text
        # After the code, before the terminal: the terminal stays last.
        assert r.text.index("give_print_code") < r.text.index("kiln3d.com/monitor") < r.text.index("kiln print benchy.3mf")

    @responses.activate
    def test_where_no_code_could_be_shown_the_page_takes_its_place(self, signed_in, model, monkeypatch):
        monkeypatch.setattr(screen_code, "_show_hook", lambda issued: False)
        _may_i()
        _held()
        r = _ask(model)
        assert "notification" not in r.text
        assert r.text.index("kiln3d.com/monitor") < r.text.index("kiln print benchy.3mf")

    def test_not_signed_in_nothing_is_posted_and_the_refusal_says_signin_once(self, model):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
            r = _ask(model)
            assert [c for c in mock.calls if "/api/print-authority/" in c.request.url] == []
        assert r.why == NOT_ASKED_CODE_SHOWN
        assert r.text.count("kiln signin") == 1 and "kiln3d.com/monitor" not in r.text
        assert r.text.index("kiln signin") < r.text.index("kiln print benchy.3mf")

    @responses.activate
    def test_a_server_that_does_not_answer_leaves_the_ladder_as_it_was(self, signed_in, model, audits):
        responses.add(responses.GET, MAY_I, body=requests.ConnectionError("down"))
        responses.add(responses.POST, PENDING, body=requests.ConnectionError("down"))
        r = _ask(model)
        assert r.consent is None and r.why == NOT_ASKED_CODE_SHOWN
        assert "kiln3d.com/monitor" not in r.text and "kiln signin" not in r.text
        assert "give_print_code" in r.text and "kiln print benchy.3mf" in r.text
        assert not [a for _, a, _ in audits if a.startswith("consent_pending")]


# ---------------------------------------------------------------------------
# The hosted server is not this door
# ---------------------------------------------------------------------------


def test_the_hosted_server_asks_no_account_and_says_what_it_said(signed_in, model, monkeypatch):
    monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        r = _ask(model)
        assert [c for c in mock.calls if "/api/print-authority/" in c.request.url] == []
    assert r.why == NOT_ASKED_HOST_CANNOT
    assert "kiln3d.com/monitor" not in r.text and "hosted server" in r.text


# ---------------------------------------------------------------------------
# The still door records where its picture is
# ---------------------------------------------------------------------------


def test_the_renderer_records_where_the_first_picture_is(tmp_path):
    from kiln.model_visualizer import visualize_model

    mesh = tmp_path / "jar.stl"
    mesh.write_text("solid jar\nendsolid jar\n")

    def _run(cmd, **kwargs):
        for i, arg in enumerate(cmd):
            if arg == "-o" and i + 1 < len(cmd):
                pathlib.Path(cmd[i + 1]).write_bytes(b"png-bytes")
        m = MagicMock()
        m.returncode = 0
        return m

    with patch("kiln.model_visualizer._find_openscad", return_value="openscad"), \
         patch("subprocess.run", side_effect=_run):
        result = visualize_model(str(mesh), output_dir=str(tmp_path / "out"), share_link=False, allow_stage=False)
    assert result["success"], result
    first = next(v["path"] for v in result["views"] if v.get("path"))
    assert preview_evidence.evidence_for(str(mesh))["png"]["picture"] == first


# ---------------------------------------------------------------------------
# "Signed out" is the session resolver's own answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tokens",
    [
        None,
        {"access_token": ""},
        {"access_token": _jwt(time.time() + 3600), "refresh_token": "rt"},
        {"access_token": _jwt(time.time() + 3600)},
        {"access_token": _jwt(time.time() + 10)},
        {"access_token": _jwt(time.time() + 10), "refresh_rejected_at": "2026-09-01T00:00:00Z"},
    ],
    ids=["no-file", "empty", "renewable", "live-no-refresh", "near-expiry", "refresh-refused"],
)
def test_signed_out_agrees_with_the_session_resolver_without_the_network(tokens, tmp_path, monkeypatch):
    def _no_network(_rt):
        raise AssertionError("network touched")

    monkeypatch.setattr(auth_session, "_post_refresh", _no_network)
    if tokens is not None:
        home = tmp_path / "auth" / ".kiln"
        home.mkdir(parents=True)
        (home / "auth_tokens.json").write_text(json.dumps(tokens))
    signed_out = bridge_client.account_signed_out()
    if tokens and tokens.get("refresh_token"):
        assert signed_out is False  # renewable: the renew is the next call's to try
    else:
        assert signed_out == (auth_session.resolve_session_bearer().token == "")


def test_picture_for_ask_reads_only_what_it_can(tmp_path):
    assert bridge_client.picture_for_ask(None) is None
    assert bridge_client.picture_for_ask(str(tmp_path / "missing.png")) is None
    (tmp_path / "junk.png").write_bytes(b"not a png")
    assert bridge_client.picture_for_ask(str(tmp_path / "junk.png")) is None
    small = _png(tmp_path, "small.png")
    assert bridge_client.picture_for_ask(str(small)) == small.read_bytes()
