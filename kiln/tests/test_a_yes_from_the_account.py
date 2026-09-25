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
import threading
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
    stage_link,
    stage_paint,
)
from kiln.preview_gate import PreviewGate
from kiln.print_consent import (
    CHOICE_THIS_PRINT,
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
from kiln.printers.base import PrinterError, PrinterState, PrinterStatus

API = "https://api.account.test"
PENDING = f"{API}/api/print-authority/pending"
MAY_I = f"{API}/api/print-authority/may-i-print"
RECORD = f"{API}/api/print-authority/record-start"
MACHINE = "ab" * 16
_REAL_PAINTER = stage_paint.try_paint_stage_views
_REAL_STAGE_LINK = stage_link.stage_link_for


def _jwt(exp: float) -> str:
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{seg({'alg': 'none'})}.{seg({'exp': exp})}.sig"


def _no_printer(printer_name=None):
    raise RuntimeError("no printer is configured in this test")


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
    # The stage painter declines unless a test hands it a real mesh and
    # puts it back: most of these files are a few bytes of junk.
    monkeypatch.setattr(stage_paint, "try_paint_stage_views", lambda *a, **k: None)
    # The card's own reads reach no printer and no link service: the printer
    # is not there, and the link door is closed.  A test of either puts its
    # own fake in place.
    monkeypatch.setattr(server, "_resolve_adapter", _no_printer)
    monkeypatch.setattr(stage_link, "stage_link_for", lambda *a, **k: None)
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


def _ask(file_name, printer_name="bench", tool="start_print", ctx=None):
    """One start attempt, judged inside its own call the way the gate reads it."""

    async def run():
        token = await server._obtain_print_consent(
            tool, {"file_name": str(file_name), "printer_name": printer_name}, ctx or types.SimpleNamespace(),
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
        # The card's fields ride only with a value: this junk 3MF carries no
        # slicer figures, no host named itself, the printer is not there to
        # read and no link was issued, so those are left out, not guessed.
        assert set(body) == {
            "file_sha256", "file_name", "printer_name", "shown_pixels_sha", "shown_door",
            "picture_png_b64", "machine_fingerprint", "observe_nonce",
            "display_name", "printer_label", *(["asked_from"] if bridge_client.asked_from() else []),
        }
        assert body["file_sha256"] == _sha(model) and body["file_name"] == "benchy.3mf"
        assert body["printer_name"] == "bench" and body["machine_fingerprint"] == MACHINE
        assert body["observe_nonce"]
        assert body["display_name"] == "benchy" and body["printer_label"] == "bench"

    @responses.activate
    def test_the_picture_the_preview_recorded_rides_the_ask_hashed_as_sent(self, signed_in, model, tmp_path, audits):
        still = _png(tmp_path, "iso.png")
        preview_evidence.record("png", str(model), renderer="stage_paint", shown_sha="abc", picture=str(still))
        _may_i()
        _held()
        _ask(model)
        assert [d["picture_source"] for _, a, d in audits if a == "consent_pending_posted"] == ["on_record"]
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
        assert refused[0]["picture_sent"] is False and refused[0]["picture_source"] == "render_failed"
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


# ---------------------------------------------------------------------------
# A still painted for the ask when none is on record
# ---------------------------------------------------------------------------


@pytest.fixture
def mesh(tmp_path):
    import trimesh

    path = tmp_path / "cube.stl"
    trimesh.creation.box(extents=(20, 20, 20)).export(str(path))
    return path


def _the_painter_can_draw(mesh_path, out_dir) -> bool:
    """Whether THIS machine's stage painter can draw at all.

    Asked only after an ask came back with no picture, so a machine that
    paints still runs every assertion and a broken wire still fails there.
    A build agent with no renderer is not a regression, and a test that
    reads it as one teaches people to ignore a red main.
    """
    try:
        from kiln.model_visualizer import _ANGLE_ROTATIONS, _CAMERA_ANGLES

        iso = next(a for a in _CAMERA_ANGLES if a[0] == "isometric")
        views = _REAL_PAINTER(
            str(mesh_path), [iso], {"isometric": _ANGLE_ROTATIONS["isometric"]},
            output_dir=str(out_dir), width=200, height=150, require_colors=False,
        )
    except Exception:  # noqa: BLE001 — a painter that raises cannot draw
        return False
    return any(v.get("path") for v in views or [])


class TestTheStillOnDemand:
    @responses.activate
    def test_none_on_record_the_stage_painter_paints_one_and_it_is_sent(self, signed_in, mesh, monkeypatch, audits, tmp_path):
        monkeypatch.setattr(stage_paint, "try_paint_stage_views", _REAL_PAINTER)
        # This is about the bytes a working painter produces, not how long the
        # ask waits for them: a loaded build machine paints slower than the
        # ask's own limit, and the ask then rightly goes without a picture.
        monkeypatch.setattr(server, "_ASK_STILL_WAIT_S", 300.0)
        _may_i()
        _held()
        _ask(mesh)
        body = json.loads(_calls(PENDING)[0].request.body)
        if not body["picture_png_b64"] and not _the_painter_can_draw(mesh, tmp_path):
            pytest.skip(
                "the real stage painter drew nothing on this machine — no renderer here (CI). "
                "The wiring is pinned by the fake-painter tests in this class; this one is about "
                "the bytes a working painter produces."
            )
        png = base64.b64decode(body["picture_png_b64"])
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) <= bridge_client.PICTURE_MAX_BYTES
        assert body["shown_pixels_sha"] == hashlib.sha256(png).hexdigest()
        assert [d["picture_source"] for _, a, d in audits if a == "consent_pending_posted"] == ["rendered"]
        # Painted for the phone, not shown here: it signs nothing off on this machine.
        assert preview_evidence.evidence_for(str(mesh))["png"] is None

    @responses.activate
    def test_the_painter_is_asked_for_the_stage_look_in_one_view(self, signed_in, mesh, monkeypatch, tmp_path):
        seen = []

        def painter(file_path, selected, rotations, **kw):
            seen.append((file_path, [a[0] for a in selected], kw))
            return [{"path": str(_png(tmp_path, "painted.png"))}]

        monkeypatch.setattr(stage_paint, "try_paint_stage_views", painter)
        _may_i()
        _held()
        _ask(mesh)
        assert len(seen) == 1 and seen[0][1] == ["isometric"] and seen[0][2]["require_colors"] is False
        assert json.loads(_calls(PENDING)[0].request.body)["picture_png_b64"]

    @responses.activate
    def test_a_painter_that_fails_posts_the_ask_without_a_picture(self, signed_in, mesh, audits):
        _may_i()
        _held("pa_2")
        r = _ask(mesh)
        body = json.loads(_calls(PENDING)[0].request.body)
        assert body["picture_png_b64"] == "" and body["shown_pixels_sha"] == ""
        assert r.why.endswith(NOT_ASKED_PENDING_TAG + "pa_2") and "kiln3d.com/monitor" in r.text
        assert [d["picture_source"] for _, a, d in audits if a == "consent_pending_posted"] == ["render_failed"]

    @responses.activate
    def test_a_painter_switched_off_is_said_as_such(self, signed_in, mesh, monkeypatch, audits):
        monkeypatch.setattr(stage_paint, "try_paint_stage_views", _REAL_PAINTER)
        monkeypatch.setenv("KILN_NO_STAGE_STILLS", "1")
        _may_i()
        _held()
        _ask(mesh)
        assert json.loads(_calls(PENDING)[0].request.body)["picture_png_b64"] == ""
        assert [d["picture_source"] for _, a, d in audits if a == "consent_pending_posted"] == ["renderer_unavailable"]

    @responses.activate
    def test_a_slow_painting_is_not_waited_for(self, signed_in, mesh, monkeypatch, tmp_path, audits):
        still = _png(tmp_path, "late.png")
        release = threading.Event()

        def slow(*a, **k):
            # A painting that does not finish until the test lets it: the ask
            # returning at all is the proof it did not wait.
            release.wait(60)
            return [{"path": str(still)}]

        monkeypatch.setattr(stage_paint, "try_paint_stage_views", slow)
        monkeypatch.setattr(server, "_ASK_STILL_WAIT_S", 0.1)
        _may_i()
        _held()
        started = time.monotonic()
        try:
            _ask(mesh)
            elapsed = time.monotonic() - started
        finally:
            release.set()
        assert elapsed < 30  # the painting would have taken 60 s
        assert json.loads(_calls(PENDING)[0].request.body)["picture_png_b64"] == ""
        assert [d["picture_source"] for _, a, d in audits if a == "consent_pending_posted"] == ["timed_out"]

    @responses.activate
    def test_an_older_server_that_wants_a_picture_leaves_no_ask_and_no_page(self, signed_in, mesh, audits):
        _may_i()
        responses.add(responses.POST, PENDING, status=400, json={"error": "picture_invalid", "message": "a picture"})
        r = _ask(mesh)
        assert NOT_ASKED_PENDING_TAG not in r.why and "kiln3d.com/monitor" not in r.text
        assert bridge_client.live_ask(_sha(mesh), "bench") is None
        assert [d["reason"] for _, a, d in audits if a == "consent_pending_refused"] == ["picture_invalid"]

    @responses.activate
    def test_nothing_is_painted_for_an_ask_already_held(self, signed_in, mesh, monkeypatch):
        painted = []
        monkeypatch.setattr(stage_paint, "try_paint_stage_views", lambda *a, **k: painted.append(1))
        _may_i()
        _held()
        _ask(mesh)
        _ask(mesh)
        assert len(painted) == 1


# ---------------------------------------------------------------------------
# Another door answered: the ask is withdrawn
# ---------------------------------------------------------------------------


def _withdraw_url(pending_id: str) -> str:
    return f"{PENDING}/{pending_id}/withdraw"


@pytest.fixture
def banners(monkeypatch):
    shown: list[screen_code.Issued] = []

    def show(issued):
        shown.append(issued)
        return True

    monkeypatch.setattr(screen_code, "_show_hook", show)
    return shown


def _give(words):
    return server.mcp._tool_manager._tools["give_print_code"].fn(words=words)


class TestTheAskIsWithdrawn:
    @responses.activate
    def test_a_typed_code_withdraws_the_ask_once(self, signed_in, model, banners, audits):
        _may_i()
        _held("pa_5")
        _ask(model)
        responses.add(responses.POST, _withdraw_url("pa_5"), json={"success": True})
        assert _give(banners[0].code)["success"]
        r = _ask(model)
        assert r.consent is not None and r.consent.source == print_consent.SOURCE_CODE
        assert len(_calls(_withdraw_url("pa_5"))) == 1
        assert _calls(_withdraw_url("pa_5"))[0].request.headers["Authorization"] == signed_in
        withdrawn = [d for _, a, d in audits if a == "consent_pending_withdrawn"]
        assert withdrawn == [{
            "file": str(model), "printer": "bench", "pending": "pa_5", "withdrawn": True, "answered_by": "screen_code",
        }]
        assert bridge_client.live_ask(_sha(model), "bench") is None

    @responses.activate
    def test_a_withdrawal_that_fails_never_stops_the_start(self, signed_in, model, banners, audits):
        _may_i()
        _held("pa_6")
        _ask(model)
        responses.add(responses.POST, _withdraw_url("pa_6"), body=requests.ConnectionError("down"))
        _give(banners[0].code)
        r = _ask(model)
        assert r.consent is not None and r.consent.source == print_consent.SOURCE_CODE
        assert [d["withdrawn"] for _, a, d in audits if a == "consent_pending_withdrawn"] == [False]

    @responses.activate
    def test_the_dialogs_yes_and_its_no_both_withdraw(self, signed_in, model, monkeypatch, audits):
        _may_i()
        _held("pa_d")
        _ask(model)
        responses.add(responses.POST, _withdraw_url("pa_d"), json={"success": True})
        monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)

        async def yes(ctx, message, **kw):
            return print_consent.DialogAnswer("accept", choice=CHOICE_THIS_PRINT)

        monkeypatch.setattr(server, "ask_user_to_confirm", yes)
        assert _ask(model).consent is not None
        assert len(_calls(_withdraw_url("pa_d"))) == 1
        # And a No, for an ask held again.
        monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: False)
        responses.replace(responses.POST, PENDING, json={"success": True, "pending": {"id": "pa_e", "expires_at": time.time() + 600, "page": "/monitor", "state": "waiting", "repeat": False}})
        _ask(model)
        responses.add(responses.POST, _withdraw_url("pa_e"), json={"success": True})
        monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)

        async def no(ctx, message, **kw):
            return print_consent.DialogAnswer("decline")

        monkeypatch.setattr(server, "ask_user_to_confirm", no)
        with pytest.raises(RuntimeError, match="declined"):
            _ask(model)
        assert len(_calls(_withdraw_url("pa_e"))) == 1
        assert [d["answered_by"] for _, a, d in audits if a == "consent_pending_withdrawn"] == ["dialog", "dialog_decline"]

    @responses.activate
    def test_a_window_that_covers_the_print_withdraws_the_ask(self, signed_in, model, audits, monkeypatch):
        _may_i()
        _held("pa_w")
        _ask(model)
        responses.add(responses.POST, _withdraw_url("pa_w"), json={"success": True})
        monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: True)  # a person opened it
        consent_windows.open_window(seconds=3600, scope=("bench",))
        _ask(model)
        assert len(_calls(_withdraw_url("pa_w"))) == 1
        assert [d["answered_by"] for _, a, d in audits if a == "consent_pending_withdrawn"] == ["standing_window"]

    @responses.activate
    def test_no_ask_held_means_no_withdrawal(self, signed_in, model, banners, audits):
        _may_i()
        responses.add(responses.POST, PENDING, status=400, json={"error": "picture_invalid"})
        _ask(model)
        _give(banners[0].code)
        assert _ask(model).consent is not None
        assert [c for c in responses.calls if c.request.url.endswith("/withdraw")] == []
        assert not [a for _, a, _ in audits if a == "consent_pending_withdrawn"]


# ---------------------------------------------------------------------------
# The card: what the ask tells the person, beside the fields that bind the yes
# ---------------------------------------------------------------------------
#
# Every card field is display only.  These pin what each one is read from,
# that each is left out when it cannot be said honestly, that none of them
# changes what the ladder decides, and that none of them posts the ask later
# than the picture's own wait allowed.

#: The moves between a slice's settings and its totals: the two filaments
#: its totals list.
_MOVES = "T0\nG1 X10 Y10 E1\nT1\nG1 X20 Y10 E1\n"

#: A painted jar's own totals, in the lines a real Kiln slice of it writes
#: at its end: two PLA filaments, 34.67 g, 1 h 50 min 26 s.
_JAR_TOTALS = (
    "; filament used [mm] = 11040.43, 584.68\n"
    "; filament used [cm3] = 26.56, 1.41\n"
    "; filament used [g] = 32.93, 1.74\n"
    "; total filament used [g] = 34.67\n"
    "; estimated printing time (normal mode) = 1h 50m 26s\n"
    "; filament_type = PLA;PLA\n"
)

#: A two-colour plate as a real Bambu print archive carries it: its totals
#: at the top of the plate's G-code, a project whose fifth filament is TPU,
#: and a plate that prints only the first two — both PLA.
_PLATE_GCODE = (
    "; HEADER_BLOCK_START\n"
    "; model printing time: 2h 6m 50s; total estimated time: 2h 14m 0s\n"
    "; total layer number: 75\n"
    "; total filament length [mm] : 5127.93,2704.70\n"
    "; total filament volume [cm^3] : 12334.13,6505.56\n"
    "; total filament weight [g] : 16.28,8.13\n"
    "; filament_density: 1.32,1.25,1.25,1.24,1.24\n"
    "; filament_diameter: 1.75,1.75,1.75,1.75,1.75\n"
    "; max_z_height: 15.00\n"
    "; filament: 1,2\n"
    "; HEADER_BLOCK_END\n\n"
    "; CONFIG_BLOCK_START\n"
    "; filament_type = PLA;PLA;PLA;PLA;TPU\n"
    "; CONFIG_BLOCK_END\n"
    "M620 S0A\nT0\nG1 X10 Y10 E1\nM620 S1A\nT1\nG1 X20 Y10 E1\n"
)
_PLATE_SLICE_INFO = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <plate>\n'
    '    <metadata key="index" value="1"/>\n'
    '    <metadata key="prediction" value="8040"/>\n'
    '    <metadata key="weight" value="24.41"/>\n'
    '    <filament id="1" type="PLA" color="#DE4343" used_m="5.13" used_g="16.28"/>\n'
    '    <filament id="2" type="PLA" color="#161616" used_m="2.70" used_g="8.13"/>\n'
    "  </plate>\n</config>\n"
)
_FIGURES = {"print_time_s", "filament_g", "material"}
_LINK = "https://app.kiln3d.com/view#v=tok"


def _jar_gcode(path: pathlib.Path) -> pathlib.Path:
    path.write_text("; filament_density: 1.24,1.24\n; filament_diameter: 1.75,1.75\n" + _MOVES + _JAR_TOTALS)
    return path


def _bambu_plate(path: pathlib.Path) -> pathlib.Path:
    import zipfile

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Metadata/plate_1.gcode", _PLATE_GCODE)
        zf.writestr("Metadata/slice_info.config", _PLATE_SLICE_INFO)
    return path


def _box(path: pathlib.Path) -> pathlib.Path:
    import trimesh

    trimesh.creation.box(extents=(20, 20, 20)).export(str(path))
    return path


def _host(name: str):
    """A handler ctx whose session carries the host's own handshake."""
    from mcp.types import InitializeRequestParams

    return types.SimpleNamespace(session=types.SimpleNamespace(
        client_params=InitializeRequestParams.model_validate({
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": name, "version": "1.0.0"},
        }),
    ))


def _posted(i: int = 0) -> dict:
    return json.loads(_calls(PENDING)[i].request.body)


class _Printer:
    """A printer that answers ``get_state`` with one reading, or raises."""

    def __init__(self, reading):
        self.reading = reading

    def get_state(self):
        if isinstance(self.reading, Exception):
            raise self.reading
        return self.reading


def _fake_view_api(monkeypatch, *, lives=(1800.0,), status=200) -> list[dict]:
    """The real link door against a fake view API: every upload is kept
    (its URL and Authorization), and answers with the next link, living the
    next of *lives* seconds (the last one repeats)."""
    import httpx

    monkeypatch.setattr(stage_link, "stage_link_for", _REAL_STAGE_LINK)
    monkeypatch.delenv(stage_link._OPT_OUT_ENV, raising=False)
    monkeypatch.setattr(stage_link, "_cache", {})
    monkeypatch.setattr(stage_link, "_REFUSED_BEARER", None)
    monkeypatch.setattr(stage_link, "_stage_printer_id", lambda: None)
    uploads: list[dict] = []

    class _Resp:
        def __init__(self, n):
            self.status_code, self.n = status, n

        def json(self):
            life = lives[min(self.n, len(lives) - 1)]
            return {"viewer_url": f"{_LINK}{self.n}", "viewer_expires_at": time.time() + life}

    def post(url, **kw):
        uploads.append({"url": f"{_LINK}{len(uploads)}", "auth": kw["headers"]["Authorization"]})
        return _Resp(len(uploads) - 1)

    monkeypatch.setattr(httpx, "post", post)
    return uploads


@pytest.fixture
def twin(tmp_path, monkeypatch):
    """The slice ledger, under tmp."""
    from kiln import monitor_twin

    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", tmp_path / "twin")
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", tmp_path / "twin" / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", tmp_path / "twin" / "active.json")
    return monitor_twin


class TestTheCard:
    @responses.activate
    def test_it_names_the_print_the_printer_and_who_asked_the_way_a_person_does(
        self, signed_in, tmp_path, monkeypatch, observed,
    ):
        model = tmp_path / "consent_test_cube.gcode.3mf"
        model.write_bytes(b"not a print archive " + str(tmp_path).encode())
        monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda name=None: name or "default")
        monkeypatch.setattr(server, "_resolve_printer_model_live", lambda name=None: "bambu_a1")
        _may_i()
        _held()
        _ask(model, printer_name=None, ctx=_host("claude-code"))
        body = _posted()
        assert body["display_name"] == "consent test cube"
        assert body["printer_label"] == "Bambu Lab A1"
        assert body["asked_by"] == "Claude"
        assert body.get("asked_from", "") == bridge_client.asked_from()
        # What binds the yes is unchanged: the exact file and Kiln's own name.
        assert body["file_name"] == "consent_test_cube.gcode.3mf" and body["printer_name"] == "default"

    @responses.activate
    def test_a_host_it_does_not_know_is_not_named_at_all(self, signed_in, model, observed):
        _may_i()
        _held()
        _ask(model, ctx=_host("some-agent-nobody-ships"))
        assert "asked_by" not in _posted()

    @responses.activate
    def test_a_slices_own_totals_say_what_yes_costs(self, signed_in, tmp_path, observed, audits):
        _may_i()
        _held()
        _ask(_jar_gcode(tmp_path / "jar.gcode"))
        body = _posted()
        assert body["print_time_s"] == 6626 and body["filament_g"] == 34.67 and body["material"] == "PLA"
        # The audit row names what the card carried.
        [row] = [d for _, a, d in audits if a == "consent_pending_posted"]
        assert {"print_time_s", "filament_g", "material", "display_name", "printer_label"} <= set(row["card"])

    @responses.activate
    def test_a_bambu_plate_is_weighed_as_the_filaments_it_prints(self, signed_in, tmp_path, observed):
        _may_i()
        _held()
        _ask(_bambu_plate(tmp_path / "chopstick_holder.gcode.3mf"))
        body = _posted()
        assert body["print_time_s"] == 8040 and body["filament_g"] == 24.41
        # The project's fifth filament is TPU; this plate never touches it.
        assert body["material"] == "PLA"

    @responses.activate
    def test_a_mesh_has_no_figures_even_where_this_machine_sliced_it(self, signed_in, tmp_path, twin, observed):
        mesh = _box(tmp_path / "jar.stl")
        gcode = _jar_gcode(tmp_path / "jar.gcode")
        twin.note_sliced(str(mesh), str(gcode))
        _may_i()
        _held()
        _ask(mesh)
        _ask(gcode)
        # Never another slice's numbers for the mesh, never a guess from its geometry...
        assert not _FIGURES & set(_posted(0))
        # ...while the slice itself, put to the account, carries its own.
        assert set(_posted(1)) >= _FIGURES

    @responses.activate
    def test_a_slice_that_names_no_material_is_not_weighed_as_a_guess(self, signed_in, tmp_path, observed):
        gcode = tmp_path / "untyped.gcode"
        gcode.write_text(_MOVES + "; filament used [mm] = 1200.00\n; estimated printing time (normal mode) = 12m 5s\n")
        _may_i()
        _held()
        _ask(gcode)
        body = _posted()
        assert body["print_time_s"] == 725
        assert "filament_g" not in body and "material" not in body

    @pytest.mark.parametrize(
        ("reading", "said"),
        [
            (PrinterState(connected=True, state=PrinterStatus.IDLE), "ready"),
            (PrinterState(connected=True, state=PrinterStatus.PRINTING), "busy"),
            (PrinterState(connected=True, state=PrinterStatus.PAUSED), "busy"),
            (PrinterState(connected=False, state=PrinterStatus.OFFLINE), "offline"),
            (PrinterState(connected=True, state=PrinterStatus.ERROR, last_known_state=PrinterStatus.PRINTING), "busy"),
            (PrinterState(connected=True, state=PrinterStatus.ERROR, last_known_state=PrinterStatus.IDLE), None),
            (PrinterState(connected=True, state=PrinterStatus.STALE, last_known_state=PrinterStatus.PRINTING), None),
            (PrinterError("Connection refused"), "offline"),
            (PrinterError("401 Unauthorized"), None),
        ],
        ids=["idle", "printing", "paused", "offline", "fault-mid-print", "fault-idle", "stale", "unreachable", "credentials"],
    )
    @responses.activate
    def test_where_the_printer_stands_is_read_and_only_shown(
        self, signed_in, model, monkeypatch, observed, reading, said,
    ):
        monkeypatch.setattr(server, "_resolve_adapter", lambda name=None: _Printer(reading))
        _may_i()
        _held()
        r = _ask(model)
        assert _posted().get("printer_state") == said
        # Shown, never decided by: the ask is held and nothing is granted,
        # whatever the printer said.
        assert r.consent is None and r.why.endswith(NOT_ASKED_PENDING_TAG + "pa_1")

    @responses.activate
    def test_the_stage_is_the_print_files_own_bytes(self, signed_in, tmp_path, monkeypatch, twin, observed):
        asked_for: list[tuple[str, dict]] = []

        def link(path, **kw):
            asked_for.append((str(path), kw))
            return {"viewer_url": _LINK, "expires_at": time.time() + 1800}

        monkeypatch.setattr(stage_link, "stage_link_for", link)
        mesh = _box(tmp_path / "cube.stl")
        gcode = _jar_gcode(tmp_path / "cube.gcode")
        twin.note_sliced(str(mesh), str(gcode))
        from kiln.printers.bambu_3mf import repackage_gcode_as_bambu_3mf

        cube = str(tmp_path / "cube.gcode.3mf")
        repackage_gcode_as_bambu_3mf(str(gcode), cube)
        _may_i()
        _held()
        _ask(mesh)
        _ask(gcode)
        _ask(cube)
        # The mesh is drawn as itself: its link rides the ask.
        assert _posted(0)["stage_url"] == _LINK
        # Raw G-code would be drawn as the mesh it came from, and this archive
        # carries only the 1 mm placeholder: neither is the bytes the ask names.
        assert "stage_url" not in _posted(1) and "stage_url" not in _posted(2)
        # Asked once, for the mesh, as the account asking, for a link that
        # outlives the ask, and with nothing put on this machine's record.
        assert asked_for == [(str(mesh), {
            "evidence": False, "bearer": signed_in.split(" ", 1)[1],
            "min_life_s": bridge_client.ASK_LIFETIME_S + 60.0,
        })]

    @responses.activate
    def test_an_obj_print_file_gets_no_stage(self, signed_in, tmp_path, monkeypatch, observed):
        """The account's stage opens STL and 3MF; an OBJ link would be one
        the card cannot draw."""
        import trimesh

        asked_for: list[str] = []
        monkeypatch.setattr(
            stage_link, "stage_link_for",
            lambda path, **kw: asked_for.append(str(path)) or {"viewer_url": _LINK, "expires_at": time.time() + 1800},
        )
        obj = tmp_path / "cube.obj"
        trimesh.creation.box(extents=(20, 20, 20)).export(str(obj))
        _may_i()
        _held()
        _ask(obj)
        assert "stage_url" not in _posted() and asked_for == []

    @responses.activate
    def test_the_stage_is_minted_for_the_account_asking(self, signed_in, tmp_path, monkeypatch, observed):
        """The account keeps a stage only when its link was minted for that
        account.  A license key in the environment is the bearer every other
        link is made with; the ask's is uploaded as the signed-in session the
        ask is posted with, and a link an earlier preview made under the
        license is never handed to it."""
        mesh = _box(tmp_path / "cube.stl")
        uploads = _fake_view_api(monkeypatch)
        monkeypatch.setenv("KILN_LICENSE_KEY", "lic-agent-key")
        agents_own = stage_link.stage_link_for(mesh)  # the agent's preview link, as the license
        _may_i()
        _held()
        _ask(mesh)
        assert [u["auth"] for u in uploads] == ["Bearer lic-agent-key", signed_in]
        assert _posted()["stage_url"] == uploads[1]["url"] != agents_own["viewer_url"]

    @responses.activate
    def test_a_link_that_would_die_before_the_ask_is_replaced_not_reused(
        self, signed_in, tmp_path, monkeypatch, observed,
    ):
        """The card loses its stage when the link runs out, and an ask lives
        ten minutes: a cached link with five minutes left is replaced, while
        one that will outlive the ask is reused as it is."""
        mesh = _box(tmp_path / "cube.stl")
        uploads = _fake_view_api(monkeypatch, lives=(300.0, 1800.0))
        _may_i()
        _held()
        _ask(mesh, printer_name="bench")
        _ask(mesh, printer_name="bench-2")  # the same bytes, asked again for another printer
        assert len(uploads) == 2
        assert _posted(1)["stage_url"] == uploads[1]["url"] != uploads[0]["url"]
        _ask(mesh, printer_name="bench-3")
        assert len(uploads) == 2 and _posted(2)["stage_url"] == uploads[1]["url"]

    @responses.activate
    def test_the_asks_link_is_not_a_preview_anyone_here_was_shown(self, signed_in, tmp_path, monkeypatch, observed):
        mesh = _box(tmp_path / "cube.stl")
        uploads = _fake_view_api(monkeypatch)
        _may_i()
        _held()
        _ask(mesh)
        assert _posted()["stage_url"] == uploads[0]["url"] and len(uploads) == 1
        # The link went to the card, not to anyone at this machine: it is no
        # preview on record, so the print gate's link door still refuses.
        evidence = preview_evidence.evidence_for(str(mesh))
        assert evidence["url"] is None and evidence["url_refusal"] is None
        refusal, _verdict = preview_evidence.judge(str(mesh), "url", host_renders=False)
        assert refusal is not None

    @responses.activate
    def test_a_link_that_cannot_be_had_leaves_no_reason_on_record_either(self, signed_in, tmp_path, monkeypatch, observed):
        mesh = _box(tmp_path / "cube.stl")
        uploads = _fake_view_api(monkeypatch, status=503)
        _may_i()
        _held()
        _ask(mesh)
        assert len(uploads) == 1 and "stage_url" not in _posted()
        # A PNG sign-off needs the link door's own refusal; the ask is not it.
        assert preview_evidence.evidence_for(str(mesh))["url_refusal"] is None

    @responses.activate
    def test_slow_reads_are_left_off_the_card_not_waited_for(self, signed_in, tmp_path, monkeypatch, observed):
        release = threading.Event()

        class _Silent:
            def get_state(self):
                release.wait(30)
                return PrinterState(connected=True, state=PrinterStatus.IDLE)

        def slow_link(path, **kw):
            release.wait(30)
            return {"viewer_url": _LINK, "expires_at": time.time() + 1800}

        monkeypatch.setattr(server, "_resolve_adapter", lambda name=None: _Silent())
        monkeypatch.setattr(stage_link, "stage_link_for", slow_link)
        monkeypatch.setattr(server, "_ASK_STILL_WAIT_S", 1.0)
        _may_i()
        _held()
        started = time.monotonic()
        try:
            r = _ask(_box(tmp_path / "cube.stl"))
            elapsed = time.monotonic() - started
        finally:
            release.set()
        # The picture's own wait is the ceiling: the reads would have taken 30 s.
        assert elapsed < 10
        body = _posted()
        assert "printer_state" not in body and "stage_url" not in body
        assert r.why.endswith(NOT_ASKED_PENDING_TAG + "pa_1")

    @responses.activate
    def test_a_printer_that_does_not_answer_is_given_three_seconds_not_the_whole_wait(
        self, signed_in, model, monkeypatch, observed,
    ):
        release = threading.Event()

        class _Silent:
            def get_state(self):
                release.wait(30)
                return PrinterState(connected=True, state=PrinterStatus.IDLE)

        monkeypatch.setattr(server, "_resolve_adapter", lambda name=None: _Silent())
        monkeypatch.setattr(server, "_ASK_PRINTER_WAIT_S", 0.2)
        monkeypatch.setattr(server, "_ASK_STILL_WAIT_S", 6.0)
        _may_i()
        _held()
        started = time.monotonic()
        try:
            _ask(model)
            elapsed = time.monotonic() - started
        finally:
            release.set()
        assert elapsed < 4.0  # the printer's own time box, not the picture's
        assert "printer_state" not in _posted()


def test_every_door_a_person_reads_names_the_print_one_way(signed_in, tmp_path, monkeypatch, observed):
    """The banner, the approval dialog and the account's card name the same
    print and the same printer the same way — never the raw file name,
    never Kiln's ``default`` alias."""
    model = tmp_path / "consent_test_cube.gcode.3mf"
    model.write_bytes(b"not a print archive " + str(tmp_path).encode())
    monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda name=None: name or "default")
    monkeypatch.setattr(server, "_resolve_printer_model_live", lambda name=None: "bambu_a1")
    shown: list[screen_code.Issued] = []
    monkeypatch.setattr(screen_code, "_show_hook", lambda issued: shown.append(issued) or True)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.add(responses.GET, MAY_I, json={"success": True, "allowed": False, "authority": None, "pending": None})
        mock.add(responses.POST, PENDING, json={"success": True, "pending": {
            "id": "pa_1", "expires_at": time.time() + 600, "page": "/monitor", "state": "waiting", "repeat": False,
        }})
        mock.add(responses.POST, f"{PENDING}/pa_1/withdraw", json={"success": True})
        _ask(model, printer_name=None)
        card = json.loads(next(c for c in mock.calls if c.request.url == PENDING).request.body)
        asked: list[str] = []

        async def no(ctx, message, **kw):
            asked.append(message)
            return print_consent.DialogAnswer("decline")

        monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)
        monkeypatch.setattr(server, "ask_user_to_confirm", no)
        with pytest.raises(RuntimeError, match="declined"):
            _ask(model, printer_name=None)
    _title, subtitle, _message = screen_code.banner_text(shown[0])
    question = asked[0].splitlines()[0]
    assert subtitle == "consent test cube on Bambu Lab A1"
    assert question == "Start printing “consent test cube” on Bambu Lab A1?"
    assert (card["display_name"], card["printer_label"]) == ("consent test cube", "Bambu Lab A1")
    for words in (subtitle, question, card["display_name"], card["printer_label"]):
        assert "default" not in words and ".3mf" not in words and "_" not in words


def test_the_card_sends_only_what_fits_the_route():
    fits = bridge_client.AskCard(
        printer_label="Bambu Lab A1", asked_by="Claude", print_time_s=2_592_000, filament_g=10_000,
        material="PLA + PETG", printer_state="busy", stage_url=_LINK,
    ).fields()
    assert fits == {
        "printer_label": "Bambu Lab A1", "asked_by": "Claude", "print_time_s": 2_592_000, "filament_g": 10_000.0,
        "material": "PLA + PETG", "printer_state": "busy", "stage_url": _LINK,
    }
    # A figure or a word out of bounds is left out rather than cut into another claim.
    for bad in (
        {"print_time_s": 0}, {"print_time_s": 2_592_001}, {"print_time_s": True}, {"print_time_s": 12.5},
        {"filament_g": 0}, {"filament_g": -3.0}, {"filament_g": 10_000.5}, {"filament_g": float("nan")},
        {"material": "PLA + PETG + TPU + ASA + PC"}, {"asked_by": "x" * 41},
        {"printer_state": "warming"}, {"stage_url": "javascript:alert(1)"}, {"stage_url": "https://a b"},
    ):
        assert set(bridge_client.AskCard(**bad).fields()) == set(), bad
    # A long name is cut on a word and marked, never past the route's 120.
    long = bridge_client.AskCard(printer_label="garage " * 30).fields()["printer_label"]
    assert len(long) <= 120 and long.endswith("…") and not long.endswith(" …")
    # One line: a name cannot carry a line break or a control character to the card.
    assert bridge_client.AskCard(printer_label="shop\nprinter\x07").fields()["printer_label"] == "shop printer"


@pytest.mark.parametrize(
    ("platform", "word"),
    [("darwin", "mac"), ("win32", "windows"), ("cygwin", "windows"), ("linux", "linux"), ("freebsd14", ""), ("emscripten", "")],
)
def test_the_computer_an_ask_comes_from(platform, word):
    assert bridge_client.asked_from(platform) == word
