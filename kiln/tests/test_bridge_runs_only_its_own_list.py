"""The bridge on a customer's computer runs only the tools on its own list.

The server decides which tools it relays, but this computer does not take its
word for it: a relayed frame naming a tool outside ``BRIDGE_ARGUMENTS``, or an
argument its entry does not name, is refused here before any fetch, any
consent, or any tool runs, and the refusal carries a code the server can
recognise.  Covers: every tool off the list (the dangerous ones by name, and
every registered one); every argument off a tool's list; the refusal's reply;
the listed tools still running; the production path from an arriving frame;
the real tool caller over the real registry; and the list matching the real
tools it names.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re

import pytest

from kiln import bridge_client
from kiln.bridge_client import (
    BRIDGE_ARGUMENTS,
    BRIDGE_REFUSED_CODE,
    BRIDGE_TOOLS,
    handle_relay_request,
)

#: Tools a server must never be able to run on a customer's computer: raw
#: G-code, heat, fans, motion, safety limits, firmware, the credential vault,
#: the install itself, consent and autonomy settings, outbound hooks, and
#: starts that bypass the saved-make print.
NEVER_OVER_THE_BRIDGE = (
    "send_gcode",
    "set_temperature",
    "set_fan",
    "home_axes",
    "park_head",
    "add_safety_profile",
    "select_printer_variant",
    "update_machine_firmware",
    "roll_back_machine_firmware",
    "store_credential",
    "retrieve_credential",
    "list_credentials",
    "list_trusted_printers",
    "upgrade_kiln",
    "restart_server",
    "set_autonomy_level",
    "revoke_consent_window",
    "register_webhook",
    "register_printer",
    "start_print",
    "upload_file",
    "download_and_upload",
    "run_quick_print",
    "print_plate_object",
    "skip_print_objects",
    "firmware_resume_print",
    "force_print_oversize",
    "clear_emergency_stop",
    "confirm_action",
    "issue_preview_token",
    "set_machine_repair",
)

#: Arguments a listed tool takes that never come over the relay: a file on
#: this computer, a skipped or forced check, a heater held or set, or a
#: person's say here.
NEVER_FROM_THE_RELAY = (
    ("cancel_print", "preserve_temperatures"),
    ("cancel_print", "expected_tool_target"),
    ("cancel_print", "expected_bed_target"),
    ("pause_print", "keep_temps"),
    ("pause_print", "leave_nozzle"),
    ("resume_print", "force"),
    ("resume_print", "hardware_confirmed"),
    ("printer_snapshot", "save_path"),
    ("set_printer_model", "slicer_file"),
    ("set_printer_model", "bed_mm"),
    ("set_printer_model", "bed_confirmed"),
    ("printer_status", "printer_names"),
    ("slice_and_print", "input_path"),
    ("slice_and_print", "profile"),
    ("slice_and_print", "printer_id"),
    ("slice_and_print", "skip_validation"),
    ("slice_and_print", "metadata"),
    ("slice_and_print", "preview_token"),
    ("slice_and_print", "placement"),
    ("printer_status", "print_authority"),
    ("pause_print", "cloud_artifact_token"),
)

#: Arguments this computer reads off the frame itself; no tool receives them.
_READ_HERE = frozenset({"cloud_artifact_token", "print_authority"})


def _approval() -> dict:
    return {"kind": "approval", "id": "pa_1", "grantor": "account:1", "door": "stage"}


def _recording_caller(recorded):
    def call_tool(name, args):
        recorded.append((name, dict(args)))
        return {"ran": name}

    return call_tool


def _recording_fetch(fetched):
    def fetch(token):
        fetched.append(token)
        return "/tmp/kiln-relay-test/model.stl"

    return fetch


def _relay(tool, args, recorded, fetched):
    return handle_relay_request(
        {"request_id": "r1", "tool_name": tool, "args": args},
        call_tool=_recording_caller(recorded),
        fetch_artifact=_recording_fetch(fetched),
    )


@pytest.fixture(scope="module")
def registry():
    from kiln import server

    return server.mcp._tool_manager._tools


class TestAToolOffTheListNeverRuns:
    @pytest.mark.parametrize("tool", NEVER_OVER_THE_BRIDGE)
    def test_even_when_the_server_names_it(self, tool):
        ran: list[str] = []
        fetched: list[str] = []
        resp = handle_relay_request(
            {
                "request_id": "r1",
                "tool_name": tool,
                "args": {"commands": "M104 S300", "cloud_artifact_token": "t", "print_authority": _approval()},
            },
            call_tool=lambda name, args: ran.append(name),
            fetch_artifact=lambda token: fetched.append(token) or "/tmp/x.stl",
        )
        assert resp["ok"] is False
        assert resp["request_id"] == "r1"
        assert resp["error"]["code"] == BRIDGE_REFUSED_CODE
        assert resp["error"]["tool"] == tool
        assert ran == [] and fetched == []

    def test_every_registered_tool_not_listed(self, registry):
        unlisted = sorted(set(registry) - set(BRIDGE_ARGUMENTS))
        assert len(unlisted) > 100, len(unlisted)
        ran: list = []
        for tool in unlisted:
            resp = handle_relay_request(
                {"request_id": tool, "tool_name": tool, "args": {}},
                call_tool=_recording_caller(ran),
                fetch_artifact=_recording_fetch([]),
            )
            assert resp["ok"] is False and resp["error"]["code"] == BRIDGE_REFUSED_CODE, tool
        assert ran == []

    def test_a_name_no_tool_has(self):
        recorded: list = []
        resp = _relay("no_such_tool", {}, recorded, [])
        assert resp["error"]["code"] == BRIDGE_REFUSED_CODE
        assert recorded == []

    def test_a_refused_call_never_sets_a_consent(self, monkeypatch):
        from kiln import print_consent

        calls: list[object] = []
        monkeypatch.setattr(print_consent, "set_consent", lambda c: calls.append(c))
        handle_relay_request(
            {"request_id": "r2", "tool_name": "send_gcode", "args": {"print_authority": _approval()}},
            call_tool=lambda name, args: None,
            fetch_artifact=lambda token: "",
        )
        assert calls == []


class TestAnArgumentOffTheListIsRefusedBeforeAnythingRuns:
    @pytest.mark.parametrize(("tool", "argument"), NEVER_FROM_THE_RELAY)
    def test_by_name(self, tool, argument):
        recorded: list = []
        fetched: list = []
        args = {"printer_name": "garage", argument: "/Users/someone/secret.3mf"}
        if tool == "slice_and_print":
            args["cloud_artifact_token"] = "tok-1"
        resp = _relay(tool, args, recorded, fetched)

        assert resp["ok"] is False
        assert resp["error"]["code"] == BRIDGE_REFUSED_CODE
        assert resp["error"]["argument"] == argument
        assert resp["error"]["tool"] == tool
        assert argument in resp["error"]["message"]
        assert recorded == [] and fetched == []

    def test_every_parameter_a_listed_tool_has_beyond_its_list(self, registry):
        checked = 0
        for tool, allowed in BRIDGE_ARGUMENTS.items():
            params = set(inspect.signature(registry[tool].fn).parameters)
            for argument in sorted(params - allowed):
                recorded: list = []
                fetched: list = []
                args = {argument: "x"}
                if "cloud_artifact_token" in allowed:
                    args["cloud_artifact_token"] = "t"
                resp = _relay(tool, args, recorded, fetched)
                assert resp["error"]["code"] == BRIDGE_REFUSED_CODE, (tool, argument)
                assert resp["error"]["argument"] == argument, (tool, argument)
                assert recorded == [] and fetched == [], (tool, argument)
                checked += 1
        assert checked >= 15, checked

    def test_arguments_that_are_not_named_values_are_refused(self):
        recorded: list = []
        resp = handle_relay_request(
            {"request_id": "r1", "tool_name": "printer_status", "args": ["printer_name"]},
            call_tool=_recording_caller(recorded),
            fetch_artifact=_recording_fetch([]),
        )
        assert resp["ok"] is False
        assert resp["error"]["code"] == BRIDGE_REFUSED_CODE
        assert recorded == []


class TestTheRefusalIsAnOrdinaryReply:
    def test_an_argument_refusal_names_it_and_that_nothing_ran(self):
        resp = _relay("cancel_print", {"preserve_temperatures": True}, [], [])

        assert resp["request_id"] == "r1"
        error = resp["error"]
        assert set(error) == {"code", "message", "tool", "argument"}
        assert "preserve_temperatures" in error["message"]
        assert "nothing was run" in error["message"].lower()
        json.dumps(resp)

    def test_a_tool_refusal_says_where_it_can_be_run(self):
        error = _relay("send_gcode", {"commands": "M104 S300"}, [], [])["error"]

        assert set(error) == {"code", "message", "tool"}
        assert "send_gcode" in error["message"]
        assert "computer" in error["message"]


class TestTheListedToolsStillRun:
    def test_a_listed_tool_still_runs(self):
        ran: list[tuple[str, dict]] = []
        resp = handle_relay_request(
            {"request_id": "r4", "tool_name": "pause_print", "args": {"printer_name": "garage"}},
            call_tool=lambda name, args: ran.append((name, args)) or {"success": True},
            fetch_artifact=lambda token: "",
        )
        assert resp["ok"] is True
        assert ran == [("pause_print", {"printer_name": "garage"})]

    def test_slice_and_print_from_a_saved_make_still_runs(self):
        ran: list[tuple[str, dict]] = []
        resp = handle_relay_request(
            {"request_id": "r5", "tool_name": "slice_and_print", "args": {"cloud_artifact_token": "tok"}},
            call_tool=lambda name, args: ran.append((name, args)) or {"success": True},
            fetch_artifact=lambda token: "/tmp/model.stl",
        )
        assert resp["ok"] is True
        assert ran[0][1]["input_path"] == "/tmp/model.stl"

    def test_each_with_every_argument_its_list_allows(self):
        for tool, allowed in BRIDGE_ARGUMENTS.items():
            recorded: list = []
            fetched: list = []
            args = {name: f"v-{name}" for name in allowed - _READ_HERE}
            if "cloud_artifact_token" in allowed:
                args["cloud_artifact_token"] = "tok-1"
            resp = _relay(tool, args, recorded, fetched)

            assert resp["ok"] is True, (tool, resp)
            ran, given = recorded[0]
            assert ran == tool
            expected = {k: v for k, v in args.items() if k not in _READ_HERE}
            if tool == "slice_and_print":
                expected["input_path"] = "/tmp/kiln-relay-test/model.stl"
                assert fetched == ["tok-1"]
            assert given == expected, tool

    def test_what_the_web_and_ai_apps_print_with(self):
        recorded: list = []
        fetched: list = []
        resp = _relay(
            "slice_and_print",
            {
                "cloud_artifact_token": "tok-1",
                "printer_name": "garage",
                "material": "PETG",
                "hardware": ["4x 6x3 magnet"],
                "color": "#ff8800",
                "design_id": "d1",
                "version_id": "v1",
                "branch_id": "b1",
                "account_sync_id": "s1",
            },
            recorded,
            fetched,
        )
        assert resp["ok"] is True, resp
        assert fetched == ["tok-1"]
        assert recorded[0][1]["input_path"] == "/tmp/kiln-relay-test/model.stl"


class TestTheProductionPath:
    def _client(self, monkeypatch, tmp_path, recorded):
        monkeypatch.setattr(bridge_client, "_STATE_FILE", str(tmp_path / "bridge.state"))
        monkeypatch.setattr(bridge_client, "_default_tool_caller", lambda: _recording_caller(recorded))
        fetched: list[str] = []
        client = bridge_client.BridgeClient(license_key="lic", fetch_artifact=_recording_fetch(fetched))
        return client, fetched

    def _send(self, client, frame):
        sent: list[dict] = []

        class _WS:
            async def send(self, text):
                sent.append(json.loads(text))

        asyncio.run(client._handle_and_reply(_WS(), frame))
        return sent

    @pytest.mark.parametrize(
        "frame",
        [
            {"request_id": "g", "tool_name": "send_gcode", "args": {"commands": "M104 S300"}},
            {"request_id": "t", "tool_name": "set_temperature", "args": {"tool_temp": 300}},
            {"request_id": "m", "tool_name": "set_printer_model", "args": {"slicer_file": "/x.3mf"}},
            {
                "request_id": "p",
                "tool_name": "slice_and_print",
                "args": {"cloud_artifact_token": "tok", "skip_validation": True},
            },
        ],
    )
    def test_a_refused_frame_reaches_no_tool_and_fetches_nothing(self, monkeypatch, tmp_path, frame):
        recorded: list = []
        client, fetched = self._client(monkeypatch, tmp_path, recorded)

        sent = self._send(client, frame)

        assert recorded == [] and fetched == []
        assert len(sent) == 1 and sent[0]["request_id"] == frame["request_id"]
        assert sent[0]["ok"] is False
        assert sent[0]["error"]["code"] == BRIDGE_REFUSED_CODE

    def test_a_listed_frame_runs(self, monkeypatch, tmp_path):
        recorded: list = []
        client, _fetched = self._client(monkeypatch, tmp_path, recorded)

        sent = self._send(client, {"request_id": "s", "tool_name": "printer_status", "args": {"detail": "lite"}})

        assert sent[0]["ok"] is True
        assert recorded == [("printer_status", {"detail": "lite"})]


class TestTheRealCaller:
    """The real tool caller over the real registry, so a second way into it
    cannot run a tool the list leaves out."""

    @pytest.fixture()
    def caller(self, monkeypatch):
        from kiln import server

        monkeypatch.setattr(server, "ensure_runtime_config", lambda: None)
        monkeypatch.setattr(server, "register_configured_printers", lambda: None)
        return bridge_client._default_tool_caller()

    def test_the_caller_refuses_a_tool_off_the_list(self, caller):
        with pytest.raises(ValueError, match="does not run send_gcode"):
            caller("send_gcode", {"commands": "M104 S300"})

    def test_through_the_handler_the_tool_body_never_runs(self, caller, monkeypatch):
        from kiln import server

        tool = server.mcp._tool_manager._tools["send_gcode"]
        ran: list[dict] = []
        monkeypatch.setattr(tool, "fn", lambda **kw: ran.append(kw) or {"success": True})
        resp = handle_relay_request(
            {"request_id": "r6", "tool_name": "send_gcode", "args": {"commands": "M104 S300"}},
            call_tool=caller,
            fetch_artifact=lambda token: "",
        )
        assert resp["ok"] is False and resp["error"]["code"] == BRIDGE_REFUSED_CODE
        assert ran == []


class TestTheListNamesRealTools:
    def test_the_tool_names_are_the_lists_keys(self):
        assert frozenset(BRIDGE_ARGUMENTS) == BRIDGE_TOOLS

    def test_every_listed_tool_and_argument_exists(self, registry):
        for tool, allowed in BRIDGE_ARGUMENTS.items():
            assert tool in registry, tool
            params = set(inspect.signature(registry[tool].fn).parameters)
            missing = sorted((allowed - _READ_HERE) - params)
            assert missing == [], (tool, missing)

    def test_no_listed_tool_is_one_that_must_never_cross(self):
        assert sorted(BRIDGE_TOOLS & set(NEVER_OVER_THE_BRIDGE)) == []

    def test_only_a_print_reads_a_file_or_a_yes_off_the_frame(self):
        carrying = sorted(tool for tool, allowed in BRIDGE_ARGUMENTS.items() if allowed & _READ_HERE)
        assert carrying == ["slice_and_print"]

    def test_the_table_cannot_be_edited_in_place(self):
        with pytest.raises(TypeError):
            BRIDGE_ARGUMENTS["send_gcode"] = frozenset({"commands"})  # type: ignore[index]


#: An argument that names a place on this computer, or that stands for the
#: person or a safety check here: a confirmation, a forced or skipped check,
#: heaters kept on.  Judged by name over every listed argument.
_LOCAL_ONLY_ARGUMENT = (
    r"(^|_)(path|paths|file|files|dir|folder|profile)$"
    r"|^force$|^skip_|_confirmed$|^preserve_|^keep_|^leave_|^bypass|^override"
    r"|^replace$|_authority$"
)

#: Arguments of that kind a relayed call still carries, each with its reason.
#: A confirmation the server carries is one a server that is not Kiln's could
#: make up; that is the separate item "print approvals the server cannot
#: forge", not a reason to break the flow below.
_CARRIED_ON_PURPOSE = {
    ("set_printer_model", "replace"): (
        "the web's model picker sends it only after the person has seen the model it replaces; "
        "the picker names a catalogue model, never a file or a bed"
    ),
    ("slice_and_print", "print_authority"): (
        "the yes a relayed print rests on, read off the frame here and never passed to the tool; "
        "a yes the server cannot make up is the separate item, print approvals the server "
        "cannot forge"
    ),
}


def test_no_listed_argument_is_one_only_this_computer_may_give():
    """No argument on the bridge's list names a place on this computer or
    stands for a person's or a safety check's say, unless it is carried on
    purpose with its reason (``_CARRIED_ON_PURPOSE``)."""
    pattern = re.compile(_LOCAL_ONLY_ARGUMENT)
    found = sorted(
        (tool, arg)
        for tool, allowed in BRIDGE_ARGUMENTS.items()
        for arg in allowed
        if pattern.search(arg) and (tool, arg) not in _CARRIED_ON_PURPOSE
    )
    assert found == [], f"listed arguments only this computer may give: {found}"
    stale = sorted(key for key in _CARRIED_ON_PURPOSE if key[1] not in BRIDGE_ARGUMENTS.get(key[0], ()))
    assert stale == [], stale


class _Server:
    """A local HTTP server that answers each path from *routes* and keeps
    every request it saw, with the Authorization header it carried."""

    def __init__(self, routes):
        import http.server
        import threading

        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen.append((self.path, self.headers.get("Authorization")))
                status, headers, body = routes(self.path)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _relay_folders():
    import tempfile
    from pathlib import Path

    return {p for p in Path(tempfile.gettempdir()).glob("kiln-relay-*")}


class TestTheDownloadOfASavedMake:
    """The bridge fetches a relayed print's model with the person's sign-in,
    against a real local server: a redirect is refused, never followed (it
    would carry that sign-in to the address it names); an answer past the
    size bound is refused; the token cannot name another path on the server;
    the call's download folder is removed when the call is over; and nothing
    but a download folder is ever removed."""

    def test_a_redirect_is_refused_and_the_other_address_sees_nothing(self, monkeypatch):
        printer = _Server(lambda path: (200, {}, b"ok"))
        api = _Server(lambda path: (302, {"Location": f"{printer.url}/rr_gcode?gcode=M104%20S280"}, b""))
        try:
            monkeypatch.setenv("KILN_API_URL", api.url)
            before = _relay_folders()
            fetch = bridge_client._default_artifact_fetcher(lambda: "bearer-of-the-person")
            with pytest.raises(ValueError, match="does not follow"):
                fetch("tok-1")
            assert printer.seen == []
            assert _relay_folders() == before
        finally:
            api.close()
            printer.close()

    def test_an_answer_past_the_bound_is_refused_and_leaves_nothing(self, monkeypatch):
        api = _Server(lambda path: (200, {}, b"x" * 5000) if path == "/api/artifact/tok-1" else (404, {}, b""))
        try:
            monkeypatch.setenv("KILN_API_URL", api.url)
            monkeypatch.setattr(bridge_client, "_ARTIFACT_MAX_BYTES", 1000)
            before = _relay_folders()
            with pytest.raises(ValueError, match="larger"):
                bridge_client._default_artifact_fetcher(lambda: "b")("tok-1")
            assert _relay_folders() == before
        finally:
            api.close()

    def test_the_token_cannot_name_another_path_on_the_server(self, monkeypatch):
        api = _Server(lambda path: (200, {}, b"solid x\nendsolid x\n") if "notes" not in path else (404, {}, b""))
        try:
            monkeypatch.setenv("KILN_API_URL", api.url)
            path = bridge_client._default_artifact_fetcher(lambda: "b")("../account/delete")
            assert api.seen[0][0] == "/api/artifact/..%2Faccount%2Fdelete"
            bridge_client._discard_fetched(path)
        finally:
            api.close()

    def test_a_calls_download_is_removed_when_the_call_is_over(self, monkeypatch, tmp_path):
        import os
        import tempfile

        made: list[str] = []

        def fetch(token):
            folder = tempfile.mkdtemp(prefix="kiln-relay-")
            path = os.path.join(folder, "model.stl")
            with open(path, "wb") as fh:
                fh.write(b"solid x\nendsolid x\n")
            made.append(path)
            return path

        ran: list = []
        monkeypatch.setattr(bridge_client, "_STATE_FILE", str(tmp_path / "bridge.state"))
        monkeypatch.setattr(bridge_client, "_default_tool_caller", lambda: _recording_caller(ran))
        client = bridge_client.BridgeClient(license_key="lic", fetch_artifact=fetch)

        reply = client._relay({"request_id": "p", "tool_name": "slice_and_print", "args": {"cloud_artifact_token": "t"}})

        assert reply["ok"] is True
        assert ran[0][1]["input_path"] == made[0]
        assert not os.path.exists(os.path.dirname(made[0]))

    def test_only_a_download_folder_is_ever_removed(self, tmp_path):
        keep = tmp_path / "kiln-relay-not-in-temp" / "model.stl"
        keep.parent.mkdir()
        keep.write_bytes(b"x")
        other = tmp_path / "mine" / "model.stl"
        other.parent.mkdir()
        other.write_bytes(b"x")
        bridge_client._discard_fetched(str(keep))
        bridge_client._discard_fetched(str(other))
        assert keep.exists() and other.exists()
