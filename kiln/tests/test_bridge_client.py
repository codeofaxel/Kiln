"""Tests for the web->printer bridge client's pure request handler.

Covers passthrough, the never-raise contract, and the print path's cloud->local
geometry resolution — no socket, cloud, or printer involved.
"""

import hashlib
import pathlib

import pytest

from kiln import print_consent
from kiln.bridge_client import handle_relay_request
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
    """A 403 loop with an expired session must say `kiln signin` — once the
    resolver is CERTAIN that is the problem.  Measured before this: 281
    rejections, every line "HTTP 403", none naming the one command that
    fixes it."""

    def _run_one_loop_iteration(self, monkeypatch, session_state, caplog):
        import asyncio
        import logging

        import kiln.bridge_client as bc
        from kiln.auth_session import SessionBearer

        class _Refused(Exception):
            def __str__(self):
                return "server rejected WebSocket connection: HTTP 403"

        class _FailingConnect:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                raise _Refused()

            async def __aexit__(self, *a):
                return False

        import types
        fake_ws = types.SimpleNamespace(connect=_FailingConnect)
        monkeypatch.setitem(__import__("sys").modules, "websockets", fake_ws)
        monkeypatch.setattr(
            "kiln.auth_session.resolve_session_bearer",
            lambda *a, **k: SessionBearer(
                token="", state=session_state, detail="run kiln signin"
            ),
        )

        client = bc.BridgeClient.__new__(bc.BridgeClient)
        client._pinned_license = "unit-test-license"
        client._url = "wss://unit.invalid/api/bridge/connect"
        client._stop = False

        async def _one_pass():
            # Stop after the first failure sleeps.
            async def _sleep(_s):
                client._stop = True

            monkeypatch.setattr(bc.asyncio, "sleep", _sleep)
            await client.run()

        with caplog.at_level(logging.DEBUG, logger="kiln.bridge_client"):
            asyncio.run(_one_pass())
        return caplog.text

    def test_needs_signin_is_said_in_plain_words(self, monkeypatch, caplog):
        text = self._run_one_loop_iteration(monkeypatch, "needs_signin", caplog)
        assert "kiln signin" in text

    def test_a_live_session_gets_no_false_signin_advice(self, monkeypatch, caplog):
        """A 403 while the session is fine (server-side refusal, an outage)
        must NOT tell the user to sign in — chasing the wrong fix hides the
        real one."""
        text = self._run_one_loop_iteration(monkeypatch, "live", caplog)
        assert "session has expired" not in text


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
