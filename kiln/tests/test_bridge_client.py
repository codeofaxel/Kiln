"""Tests for the web->printer bridge client's pure request handler.

Covers passthrough, the never-raise contract, and the print path's cloud->local
geometry resolution — no socket, cloud, or printer involved.
"""

from kiln.bridge_client import handle_relay_request


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
