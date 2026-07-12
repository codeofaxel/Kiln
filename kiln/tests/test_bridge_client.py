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
