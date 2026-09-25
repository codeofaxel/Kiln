"""Tests for the web->printer bridge client's pure request handler.

Covers passthrough, the never-raise contract, and the print path's cloud->local
geometry resolution — no socket, cloud, or printer involved.
"""

import hashlib
import pathlib

import pytest

from kiln import print_consent
from kiln.bridge_client import handle_relay_request, observe_addresses
from kiln.print_consent import SOURCE_HOSTED_APPROVAL, SOURCE_HOSTED_DELEGATION


def _recording_caller(recorded):
    def call_tool(name, args):
        recorded.append((name, dict(args)))
        return {"ran": name}

    return call_tool


def _never_fetch(token):
    raise AssertionError("fetch_artifact should not be called")


def test_passthrough_tool_runs_and_reports_ok():
    recorded = []
    resp = handle_relay_request(
        {"request_id": "r1", "tool_name": "printer_status", "args": {"printer_name": "x"}},
        call_tool=_recording_caller(recorded),
        fetch_artifact=_never_fetch,
    )
    assert resp["ok"] is True
    assert resp["request_id"] == "r1"
    assert recorded == [("printer_status", {"printer_name": "x"})]


def test_tool_error_becomes_a_closed_error_not_a_raise():
    def boom(name, args):
        raise RuntimeError("printer offline")

    resp = handle_relay_request(
        {"request_id": "r2", "tool_name": "printer_status", "args": {}},
        call_tool=boom,
        fetch_artifact=_never_fetch,
    )
    assert resp["ok"] is False
    assert "printer offline" in resp["error"]["message"]
    assert resp["error"]["tool"] == "printer_status"


def test_print_resolves_cloud_artifact_to_a_local_path():
    recorded = []

    def fetch(token):
        assert token == "tok-123"
        return "/tmp/mesh.stl"

    resp = handle_relay_request(
        {
            "request_id": "r3",
            "tool_name": "slice_and_print",
            "args": {"cloud_artifact_token": "tok-123", "printer_name": "p1"},
        },
        call_tool=_recording_caller(recorded),
        fetch_artifact=fetch,
    )
    assert resp["ok"] is True
    name, args = recorded[0]
    assert name == "slice_and_print"
    assert args["input_path"] == "/tmp/mesh.stl"  # resolved geometry
    assert "cloud_artifact_token" not in args  # the cloud ref never reaches the tool


def test_print_fetch_failure_is_reported_not_raised():
    def fetch(token):
        raise RuntimeError("artifact expired")

    resp = handle_relay_request(
        {
            "request_id": "r4",
            "tool_name": "slice_and_print",
            "args": {"cloud_artifact_token": "gone"},
        },
        call_tool=_recording_caller([]),
        fetch_artifact=fetch,
    )
    assert resp["ok"] is False
    assert "artifact expired" in resp["error"]["message"]


def test_local_slice_and_print_does_not_trigger_a_fetch():
    # A local-path slice_and_print (not from the web) is a plain passthrough.
    recorded = []
    resp = handle_relay_request(
        {
            "request_id": "r5",
            "tool_name": "slice_and_print",
            "args": {"input_path": "/local/a.stl"},
        },
        call_tool=_recording_caller(recorded),
        fetch_artifact=_never_fetch,
    )
    assert resp["ok"] is True
    assert recorded[0][1] == {"input_path": "/local/a.stl"}


class TestHandshake403NamesTheFix:
    """A 403 loop with a dead session must say `kiln signin` — once the
    resolver is CERTAIN that is the problem.  Measured before this: 281
    rejections, every line "HTTP 403", none naming the one command that
    fixes it.

    Then measured again, 2026-09-24: 591 rejections with NO hint, because
    the resolver's fast path judges by the token's clock and the clock
    cannot see a session revoked server-side (``exp`` was forty minutes
    out; GET /auth/v1/user said ``session_not_found``).  So a SUSTAINED
    refusal now asks the server once — one refresh exchange — and a
    session it will not renew is said once and retried in minutes.
    """

    class _Refused(Exception):
        def __str__(self):
            return "server rejected WebSocket connection: HTTP 403"

    def _refusing_relay(self, monkeypatch):
        import types

        refused = self._Refused

        class _FailingConnect:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                raise refused()

            async def __aexit__(self, *a):
                return False

        fake_ws = types.SimpleNamespace(connect=_FailingConnect)
        monkeypatch.setitem(__import__("sys").modules, "websockets", fake_ws)

    def _drive(self, monkeypatch, caplog, client, iterations):
        """Run *client* through *iterations* refusals; return log text + sleeps."""
        import asyncio
        import logging

        import kiln.bridge_client as bc

        sleeps: list[float] = []

        async def _passes():
            async def _sleep(s):
                sleeps.append(s)
                if len(sleeps) >= iterations:
                    client._stop = True

            monkeypatch.setattr(bc.asyncio, "sleep", _sleep)
            await client.run()

        with caplog.at_level(logging.DEBUG, logger="kiln.bridge_client"):
            asyncio.run(_passes())
        return caplog.text, sleeps

    def _run(self, monkeypatch, caplog, *, plain, verified, iterations):
        """A session-bearer bridge through *iterations* refusals.

        *plain* is what the resolver answers when asked by the clock alone,
        *verified* what it answers when asked to check with the server.
        Returns the log text, the sleeps taken, and how many times the
        server was asked.
        """
        import kiln.bridge_client as bc
        from kiln.auth_session import SessionBearer

        self._refusing_relay(monkeypatch)
        asked_server: list[bool] = []
        on_file: list[str] = []  # the real resolver persists a rejection

        def _resolve(*a, verify=False, **k):
            if verify:
                asked_server.append(True)
                state = verified
                if state == "needs_signin":
                    on_file.append(state)
            else:
                state = on_file[-1] if on_file else plain
            token = "tok" if state in ("live", "refreshed", "degraded") else ""
            return SessionBearer(
                token=token, state=state, detail="Your Kiln session has expired."
            )

        monkeypatch.setattr("kiln.auth_session.resolve_session_bearer", _resolve)
        # The bearer the loop presents is the session's, not a pinned
        # license: a pinned license is never asked about.
        monkeypatch.setattr(bc, "_read_license", lambda **k: "tok")

        client = bc.BridgeClient.__new__(bc.BridgeClient)
        client._pinned_license = None
        client._url = "wss://unit.invalid/api/bridge/connect"
        client._stop = False
        text, sleeps = self._drive(monkeypatch, caplog, client, iterations)
        return text, sleeps, len(asked_server)

    def test_needs_signin_is_said_in_plain_words(self, monkeypatch, caplog):
        text, sleeps, asked = self._run(
            monkeypatch, caplog, plain="needs_signin", verified="needs_signin", iterations=1
        )
        assert "kiln signin" in text
        assert asked == 0, "a verdict already on file needs no exchange"

    def test_a_sustained_403_on_a_clock_live_session_asks_the_server_once(
        self, monkeypatch, caplog
    ):
        """Today's case: the clock says live, the relay says no, the
        server, asked, says the session is gone.  Said once; then minutes."""
        import kiln.bridge_client as bc

        text, sleeps, asked = self._run(
            monkeypatch, caplog, plain="live", verified="needs_signin", iterations=4
        )
        assert asked == 1, "one exchange per outage, not one per refusal"
        assert text.count("kiln signin") == 1, "said once, not per retry"
        # The first refusal alone is not sustained: ordinary backoff.
        assert sleeps[0] == 1.0
        # From the verdict on, the storm stops: minutes between attempts.
        assert sleeps[1:] == [bc.SIGNED_OUT_RETRY_S] * 3
        assert bc.SIGNED_OUT_RETRY_S >= 60.0

    def test_a_live_session_gets_no_false_signin_advice(self, monkeypatch, caplog):
        """A 403 while the session is fine (server-side refusal, an outage)
        must NOT tell the user to sign in — chasing the wrong fix hides the
        real one.  The server IS asked, once, and its renewal is the proof
        the session is fine; the reconnect keeps the ordinary backoff."""
        text, sleeps, asked = self._run(
            monkeypatch, caplog, plain="live", verified="refreshed", iterations=4
        )
        assert asked == 1
        assert "kiln signin" not in text
        assert "renewed the session" in text
        assert max(sleeps) <= 60.0

    def test_a_probe_that_got_no_verdict_is_asked_again_after_the_retry_interval(
        self, monkeypatch, caplog
    ):
        """The server, asked, did not answer (a proxy 502, a 429, a network
        blip): that is not a verdict, and treating it as "probed" left a
        dead session retrying every 60 s for ever with no "run kiln signin".
        The question is put again once the signed-out retry interval has
        been slept away, and the second answer is a verdict."""
        import kiln.bridge_client as bc
        from kiln.auth_session import SessionBearer

        self._refusing_relay(monkeypatch)
        verified = iter(["degraded", "needs_signin"])
        asked: list[bool] = []

        def _resolve(*a, verify=False, **k):
            if verify:
                asked.append(True)
                state = next(verified, "needs_signin")
            else:
                state = "needs_signin" if len(asked) >= 2 else "live"
            token = "tok" if state in ("live", "refreshed", "degraded") else ""
            return SessionBearer(token=token, state=state, detail="Your Kiln session has expired.")

        monkeypatch.setattr("kiln.auth_session.resolve_session_bearer", _resolve)
        monkeypatch.setattr(bc, "_read_license", lambda **k: "tok")
        client = bc.BridgeClient.__new__(bc.BridgeClient)
        client._pinned_license = None
        client._url = "wss://unit.invalid/api/bridge/connect"
        client._stop = False
        text, sleeps = self._drive(monkeypatch, caplog, client, 12)

        assert len(asked) == 2, f"asked {len(asked)} times; sleeps {sleeps}"
        assert "kiln signin" in text
        assert text.count("kiln signin") == 1
        # The re-ask waited the signed-out interval out, not a single backoff.
        first_answer = sleeps.index(bc.SIGNED_OUT_RETRY_S)
        assert sum(sleeps[1:first_answer]) >= bc.SIGNED_OUT_RETRY_S
        # And from the verdict on, the storm stops.
        assert sleeps[first_answer:] == [bc.SIGNED_OUT_RETRY_S] * len(sleeps[first_answer:])

    def test_a_pinned_license_is_never_asked_about(self, monkeypatch, caplog):
        import kiln.bridge_client as bc

        self._refusing_relay(monkeypatch)
        asked = []
        monkeypatch.setattr(
            "kiln.auth_session.resolve_session_bearer",
            lambda *a, **k: asked.append(k) or None,
        )
        client = bc.BridgeClient.__new__(bc.BridgeClient)
        client._pinned_license = "unit-test-license"
        client._url = "wss://unit.invalid/api/bridge/connect"
        client._stop = False

        text, sleeps = self._drive(monkeypatch, caplog, client, 3)

        assert asked == []
        assert "kiln signin" not in text
        assert sleeps == [1.0, 2.0, 4.0]


# ---------------------------------------------------------------------------
# A relayed start carries the hosted authority — the person's approval, or
# the delegation an agent prints under — and the bridge turns it into the
# consent the local gate reads, after checking the bytes are the ones approved.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_consent_leaks():
    print_consent._reset_for_tests()
    yield
    print_consent._reset_for_tests()


def _mesh(tmp_path: pathlib.Path) -> tuple[str, str]:
    path = tmp_path / "jar.stl"
    path.write_bytes(b"\x00" * 84 + b"\x01" * 50)
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


def _approval(sha: str, **over):
    block = {
        "kind": "approval",
        "id": "apv_1",
        "grantor": "account:acct_123",
        "said_go_by": "account:acct_123",
        "file_sha256": sha,
        "printer_name": "garage",
        "door": "web_stage",
        "until": 4102444800.0,
    }
    block.update(over)
    return block


def _consent_seen_by_tool(seen):
    def call_tool(name, args):
        seen.append((name, dict(args), print_consent._current.get()))
        return {"ran": name}

    return call_tool


def test_a_relayed_approval_becomes_the_consent_for_the_call(tmp_path):
    path, sha = _mesh(tmp_path)
    seen = []
    resp = handle_relay_request(
        {
            "request_id": "r6",
            "tool_name": "slice_and_print",
            "args": {
                "cloud_artifact_token": "tok",
                "printer_name": "garage",
                "print_authority": _approval(sha),
            },
        },
        call_tool=_consent_seen_by_tool(seen),
        fetch_artifact=lambda _t: path,
    )
    assert resp["ok"] is True, resp
    name, args, consent = seen[0]
    assert "print_authority" not in args  # never reaches the tool's signature
    assert consent is not None
    assert consent.source == SOURCE_HOSTED_APPROVAL
    assert consent.identity == "account:acct_123#apv_1"
    assert consent.printer_name == "garage"
    assert consent.door == "web_stage"
    assert consent.matches(file_name=path, printer_name="garage")
    # The yes lives exactly as long as the call.
    assert print_consent._current.get() is None


def test_an_agent_starting_under_the_print_the_person_approved_at_its_asking_is_named(tmp_path):
    """A person's one-print approval answered to an agent's question: the
    yes is the person's, the start is the agent's, and the clearance says
    both — never the person alone."""
    path, sha = _mesh(tmp_path)
    seen = []
    resp = handle_relay_request(
        {
            "request_id": "r6b",
            "tool_name": "slice_and_print",
            "args": {
                "cloud_artifact_token": "tok",
                "printer_name": "garage",
                "print_authority": _approval(sha, said_go_by="agent:openclaw/igor#K7Q2"),
            },
        },
        call_tool=_consent_seen_by_tool(seen),
        fetch_artifact=lambda _t: path,
    )
    assert resp["ok"] is True, resp
    _, _, consent = seen[0]
    assert consent.source == SOURCE_HOSTED_APPROVAL
    assert consent.identity == "agent:openclaw/igor#K7Q2 under account:acct_123#apv_1"


def test_a_call_that_names_no_printer_gets_a_consent_aimed_at_none(tmp_path):
    """The gate matches a consent against the name the call used; the
    hosted server already held the record to the printer it was made
    for, so a call aimed at the default printer is not refused for
    naming none."""
    path, sha = _mesh(tmp_path)
    seen = []
    resp = handle_relay_request(
        {
            "request_id": "r6c",
            "tool_name": "slice_and_print",
            "args": {"cloud_artifact_token": "tok", "print_authority": _approval(sha, printer_name="default")},
        },
        call_tool=_consent_seen_by_tool(seen),
        fetch_artifact=lambda _t: path,
    )
    assert resp["ok"] is True, resp
    consent = seen[0][2]
    assert consent.printer_name is None
    assert consent.matches(file_name=path, printer_name=None)


def test_a_relayed_delegation_names_the_agent_and_carries_its_scope(tmp_path):
    path, sha = _mesh(tmp_path)
    seen = []
    resp = handle_relay_request(
        {
            "request_id": "r7",
            "tool_name": "slice_and_print",
            "args": {
                "cloud_artifact_token": "tok",
                "printer_name": "garage",
                "print_authority": _approval(
                    sha, kind="delegation", id="dlg_9",
                    said_go_by="agent:openclaw/igor#K7Q2",
                    printers=["garage", "workshop"],
                ),
            },
        },
        call_tool=_consent_seen_by_tool(seen),
        fetch_artifact=lambda _t: path,
    )
    assert resp["ok"] is True, resp
    _, _, consent = seen[0]
    assert consent.source == SOURCE_HOSTED_DELEGATION
    assert consent.identity == "agent:openclaw/igor#K7Q2 under account:acct_123#dlg_9"
    assert consent.scope == ("garage", "workshop")
    assert consent.expires_at == 4102444800.0


def test_bytes_other_than_the_approved_ones_are_refused_before_the_tool(tmp_path):
    path, _sha = _mesh(tmp_path)
    seen = []
    resp = handle_relay_request(
        {
            "request_id": "r8",
            "tool_name": "slice_and_print",
            "args": {
                "cloud_artifact_token": "tok",
                "printer_name": "garage",
                "print_authority": _approval("ab" * 32),
            },
        },
        call_tool=_consent_seen_by_tool(seen),
        fetch_artifact=lambda _t: path,
    )
    assert resp["ok"] is False
    assert seen == []
    assert "approved" in resp["error"]["message"].lower()


def test_a_relayed_call_without_authority_runs_with_no_consent(tmp_path):
    """Unchanged: the local gate then refuses it in its own words."""
    path, _sha = _mesh(tmp_path)
    seen = []
    resp = handle_relay_request(
        {
            "request_id": "r9",
            "tool_name": "slice_and_print",
            "args": {"cloud_artifact_token": "tok", "printer_name": "garage"},
        },
        call_tool=_consent_seen_by_tool(seen),
        fetch_artifact=lambda _t: path,
    )
    assert resp["ok"] is True
    assert seen[0][2] is None


def test_a_malformed_authority_is_dropped_not_trusted(tmp_path):
    path, sha = _mesh(tmp_path)
    seen = []
    resp = handle_relay_request(
        {
            "request_id": "r10",
            "tool_name": "slice_and_print",
            "args": {
                "cloud_artifact_token": "tok",
                "printer_name": "garage",
                "print_authority": _approval(sha, kind="wish"),
            },
        },
        call_tool=_consent_seen_by_tool(seen),
        fetch_artifact=lambda _t: path,
    )
    assert resp["ok"] is True
    assert "print_authority" not in seen[0][1]
    assert seen[0][2] is None


# ---------------------------------------------------------------------------
# Showing the relay this machine's other side
# ---------------------------------------------------------------------------


def test_the_bridge_shows_each_address_family_it_has_and_shrugs_at_the_rest():
    import socket

    asked = []

    def post(family):
        asked.append(family)
        if family == socket.AF_INET6:
            raise OSError("no IPv6 here")
        return True

    shown = observe_addresses("https://api.example", "bearer", "nonce-1", post=post)
    assert asked == [socket.AF_INET, socket.AF_INET6]
    assert shown == {"v4": True, "v6": False}


def test_the_relay_observe_frame_is_answered_off_to_the_side_never_as_a_tool(monkeypatch):
    import asyncio

    from kiln import bridge_client
    from kiln.bridge_client import BridgeClient

    observed = []
    monkeypatch.setattr(
        bridge_client, "observe_addresses",
        lambda api, bearer, nonce, **kw: observed.append((api, bearer, nonce)) or {"v4": True, "v6": False},
    )
    ran = []
    sent = []

    class _WS:
        async def send(self, text):
            sent.append(text)

    client = BridgeClient(
        license_key="bearer-x",
        call_tool=lambda name, args: ran.append(name) or {"ok": True},
        fetch_artifact=_never_fetch,
    )

    async def scenario():
        t1 = client._dispatch_frame(_WS(), {"observe_nonce": "nonce-1"})
        t2 = client._dispatch_frame(_WS(), {"request_id": "r1", "tool_name": "printer_status", "args": {}})
        assert client._dispatch_frame(_WS(), ["not", "a", "frame"]) is None
        await asyncio.gather(t1, t2)

    asyncio.run(scenario())
    assert observed == [("https://api.kiln3d.com", "bearer-x", "nonce-1")]
    assert ran == ["printer_status"]
    assert len(sent) == 1  # the tool's reply; the observation sends nothing down the socket
