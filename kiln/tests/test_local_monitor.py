"""Kiln's inline print monitor, served by a local install.

The properties worth defending mirror the stage's, plus this door's own:

* it needs nothing but public Kiln;
* it is ON without anyone setting a flag;
* the payload rides the result ONLY for a host that renders panels, and
  ONLY on roster tools — this hook does printer I/O, so an unknown tool
  must skip, the OPPOSITE of the stage's fail-open rule;
* the payload is LEAN: the readings ride, the camera frame never does —
  the panel fetches it through ``kiln_monitor_snapshot``, which stands on
  the tool list from install for exactly that reason;
* the wire it speaks is ``kiln.monitor.v1`` from its one home,
  ``kiln.monitor_payload``;
* the account axis reports the truth and gates nothing but the rendering —
  the text report stays whole either way.
"""

from __future__ import annotations

import json

import anyio
import pytest

from kiln import local_monitor, monitor_payload, stage_cache

_DOC = "<!DOCTYPE html><html><body>monitor</body></html>"


class _Caps:
    def __init__(self, extensions=None, experimental=None):
        self.experimental = experimental
        self.model_extra = {"extensions": extensions} if extensions is not None else {}


class _Host:
    """Just enough of a FastMCP to answer "what did the client declare?"."""

    def __init__(self, caps=None, name="TestHost"):
        session = type("S", (), {})()
        session.client_params = type("P", (), {})()
        session.client_params.capabilities = caps
        session.client_params.clientInfo = type(
            "I", (), {"name": name, "version": "1"}
        )()
        ctx = type("C", (), {})()
        ctx.session = session
        self._mcp_server = type("L", (), {"request_context": ctx})()


_UI = "io.modelcontextprotocol/ui"


def _apps_host():
    return _Host(_Caps(extensions={_UI: {}}))


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    from kiln import local_stage

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln_home"))
    monkeypatch.delenv(local_monitor._OPT_OUT_ENV, raising=False)
    monkeypatch.delenv(local_monitor._INLINE_CAMERA_ENV, raising=False)
    local_monitor._reset_for_tests()
    local_stage._reset_for_tests()
    stage_cache._reset_for_tests()
    yield
    local_monitor._reset_for_tests()
    local_stage._reset_for_tests()
    stage_cache._reset_for_tests()


@pytest.fixture(autouse=True)
def _stub_composition(monkeypatch):
    """Every test speaks to a fake printer unless it opts into its own.

    The status axis normally calls the real ``printer_status`` (a lazy
    ``kiln.server`` import); tests stub the seam above it so no test here
    pays that import or needs an adapter.
    """
    monkeypatch.setattr(
        local_monitor,
        "_direct_status",
        lambda printer_name, detail="lite": (
            {
                "success": True,
                "printer": {"state": "printing", "connected": True},
                "job": {"file_name": "benchy.gcode", "completion": 41.0},
                **({"printer_name": printer_name} if printer_name else {}),
            },
            None,
        ),
    )
    monkeypatch.setattr(
        local_monitor, "_camera_frame", lambda printer_name, status: (None, "no camera available")
    )
    monkeypatch.setattr(local_monitor, "_signed_in", lambda: True)


def _cache_the_monitor():
    (stage_cache.cache_dir() / "print_monitor.html").write_text(
        _DOC, encoding="utf-8"
    )
    stage_cache._reset_for_tests()


def _fastmcp():
    from kiln.mcp_compat import FastMCP

    mcp = FastMCP("test")

    @mcp.tool(name="monitor_print")
    def monitor_print(printer_name: str | None = None) -> str:
        """One-shot print status report."""
        return "PRINTING 41%"

    @mcp.tool(name="list_materials")
    def list_materials() -> dict:
        return {"success": True}

    return mcp


class TestNeedsNothingButPublicKiln:
    def test_the_module_does_not_reach_into_kiln_pro(self):
        src = (
            __import__("pathlib")
            .Path(local_monitor.__file__)
            .read_text(encoding="utf-8")
        )
        assert "kiln_pro" not in src


class TestOnByDefault:
    def test_enabled_with_no_flag_set(self):
        assert local_monitor.enabled() is True

    def test_opt_out_turns_everything_off(self, monkeypatch):
        monkeypatch.setenv(local_monitor._OPT_OUT_ENV, "1")
        assert local_monitor.enabled() is False
        assert local_monitor.install(object()) == {  # would explode if it did anything
            "enabled": False, "resource": False, "snapshot_tool": False, "control_tool": False, "stamped": 0
        }


class TestInstallOnARealFastMCP:
    def test_registers_the_resource_and_stamps_only_monitor_tools(self):
        _cache_the_monitor()
        mcp = _fastmcp()
        out = local_monitor.install(mcp)
        assert out["resource"] and out["hook"] and out["stamped"] == 1
        tools = mcp._tool_manager._tools
        assert (tools["monitor_print"].meta or {})["ui"]["resourceUri"] == (
            local_monitor.PRINT_MONITOR_RESOURCE_URI
        )
        assert not (tools["list_materials"].meta or {}).get("ui")

    def test_stamped_tools_say_so_in_their_descriptions(self):
        """The clause is the searchable surface of the capability — the
        stage's 2026-08-02 lesson, applied at this door's conception."""
        _cache_the_monitor()
        mcp = _fastmcp()
        local_monitor.install(mcp)
        desc = mcp._tool_manager._tools["monitor_print"].description or ""
        assert local_monitor.MONITOR_DESCRIPTION_CLAUSE in desc
        for keyword in ("LIVE MONITOR", "inline", "panel", "camera"):
            assert keyword in desc, f"monitor clause not findable by {keyword!r}"
        assert desc.index(local_monitor.MONITOR_DESCRIPTION_CLAUSE) > 0

    def test_a_second_install_does_not_stutter_the_clause(self):
        _cache_the_monitor()
        mcp = _fastmcp()
        local_monitor.install(mcp)
        local_monitor.install(mcp)
        desc = mcp._tool_manager._tools["monitor_print"].description or ""
        assert desc.count(local_monitor.MONITOR_DESCRIPTION_CLAUSE) == 1

    def test_a_cold_cache_still_installs(self):
        mcp = _fastmcp()
        out = local_monitor.install(mcp)
        assert out["resource"] and out["stamped"] == 1


class TestTheMonitorDocumentComesFromTheCache:
    def test_reading_the_resource_serves_the_cached_document(self):
        _cache_the_monitor()
        mcp = _fastmcp()
        local_monitor.install(mcp)
        got = anyio.run(mcp.read_resource, local_monitor.PRINT_MONITOR_RESOURCE_URI)
        text = getattr(got[0], "content", got[0]) if isinstance(got, list) else got
        assert _DOC in str(text)

    def test_the_read_keeps_the_poll_verb_it_already_has(self):
        """The verb stood from install; the read's belt-and-braces call
        must not register a second one under the panel."""
        _cache_the_monitor()
        mcp = _fastmcp()
        local_monitor.install(mcp)
        before = mcp._tool_manager._tools["kiln_monitor_snapshot"]
        anyio.run(mcp.read_resource, local_monitor.PRINT_MONITOR_RESOURCE_URI)
        assert mcp._tool_manager._tools["kiln_monitor_snapshot"] is before

    def test_a_cold_cache_read_raises_and_the_verb_still_stands(self):
        mcp = _fastmcp()
        local_monitor.install(mcp)
        # The SDK wraps the door's ValueError in its own resource error --
        # 1.x quotes the sentence, 2.x says "Error reading resource" and
        # chains the cause -- so the sentence is looked for down the chain.
        with pytest.raises(Exception) as excinfo:
            anyio.run(mcp.read_resource, local_monitor.PRINT_MONITOR_RESOURCE_URI)
        chain, err = [], excinfo.value
        while err is not None and len(chain) < 8:
            chain.append(str(err))
            err = err.__cause__ or err.__context__
        assert any("not been downloaded" in text for text in chain), chain
        assert "kiln_monitor_snapshot" in mcp._tool_manager._tools


class TestComposeLocalPayload:
    def test_direct_transport_and_wire_identity(self):
        payload = local_monitor.compose_local_payload()
        assert payload["kind"] == monitor_payload.MONITOR_PAYLOAD_KIND
        assert payload["bridge"] == {
            "online": True,
            "paired": True,
            "lastSeenAt": None,
            "transport": "direct",
        }
        assert payload["status"]["printer"]["state"] == "printing"
        assert payload["account"] == {"signed_in": True}

    def test_a_named_printer_threads_through_for_the_panels_own_polls(self):
        payload = local_monitor.compose_local_payload(printer_name="workshop-a1")
        assert payload["printer_name_arg"] == "workshop-a1"
        assert "printer_name_arg" not in local_monitor.compose_local_payload()

    def test_a_status_refusal_becomes_the_structured_failure_axis(self, monkeypatch):
        monkeypatch.setattr(
            local_monitor,
            "_direct_status",
            lambda printer_name, detail="lite": (
                None,
                {"code": "NOT_FOUND", "message": "Printer 'x' not found."},
            ),
        )
        payload = local_monitor.compose_local_payload(printer_name="x")
        assert "status" not in payload
        assert payload["status_failure"]["code"] == "NOT_FOUND"

    def test_signed_out_reports_honestly(self, monkeypatch):
        monkeypatch.setattr(local_monitor, "_signed_in", lambda: False)
        payload = local_monitor.compose_local_payload()
        assert payload["account"] == {"signed_in": False}
        # The readings themselves are NOT withheld — the account axis gates
        # the rendering, never the truth.
        assert payload["status"]["printer"]["state"] == "printing"

    def test_camera_only_rides_when_asked(self, monkeypatch):
        monkeypatch.setattr(
            local_monitor, "_camera_frame", lambda pn, status: ("QUJD", None)
        )
        with_cam = local_monitor.compose_local_payload(include_camera=True)
        without = local_monitor.compose_local_payload(include_camera=False)
        assert with_cam["camera"] == {"image_base64": "QUJD"}
        assert "camera" not in without


class TestTheAccountAxisAsksForAnAccountNotACredential:
    """The rope must fire for a stranger and never for a member.

    Measured 2026-08-25: a signed-in enterprise account whose refresh had
    been rejected the day before resolved to NO bearer.  Gating on the
    bearer would have shown the panel's own owner the sign-in
    invitation — and would rope every user the moment they went offline,
    in a panel that is direct and never calls the API at all.
    """

    _real_signed_in = staticmethod(local_monitor._signed_in)

    @pytest.fixture(autouse=True)
    def _isolated_auth(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
        monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
        (tmp_path / ".kiln").mkdir(parents=True, exist_ok=True)
        return tmp_path / ".kiln" / "auth_tokens.json"

    def test_no_account_on_this_machine_is_signed_out(self):
        assert self._real_signed_in() is False

    def test_a_completed_signin_is_signed_in(self, _isolated_auth):
        _isolated_auth.write_text(
            json.dumps({"auth_uid": "abc", "email": "a@b.com"}), encoding="utf-8"
        )
        assert self._real_signed_in() is True

    def test_a_lapsed_session_is_still_an_account(self, _isolated_auth):
        """The regression this class exists for: refresh rejected, access
        token stale — still the same person, still their panel."""
        _isolated_auth.write_text(
            json.dumps(
                {
                    "auth_uid": "abc",
                    "email": "adam@example.com",
                    "tier": "enterprise",
                    "refresh_rejected_at": "2026-08-24T22:06:33Z",
                }
            ),
            encoding="utf-8",
        )
        assert self._real_signed_in() is True

        from kiln.auth_session import resolve_api_bearer

        assert not resolve_api_bearer().token, (
            "precondition: this state has no usable bearer — which is "
            "exactly why the axis must not ask for one"
        )

    def test_an_operator_license_counts_as_an_account(self, monkeypatch):
        monkeypatch.setenv("KILN_LICENSE_KEY", "kiln_live_whatever")
        assert self._real_signed_in() is True

    def test_a_corrupt_token_file_reads_as_signed_out_without_raising(
        self, _isolated_auth
    ):
        _isolated_auth.write_text("{not json", encoding="utf-8")
        assert self._real_signed_in() is False


class TestCapabilitiesRideOnlyOnThePollThatAsks:
    """The panel asks for the machine's capabilities once per watched print
    (``include_capabilities``); that poll reads the full status shape, every
    other poll stays lite.  Driven through the REAL ``_direct_status`` so the
    ``detail`` handed to ``printer_status`` is the thing under test."""

    _real_direct_status = staticmethod(local_monitor._direct_status)

    def test_the_asking_poll_reads_full_and_the_rest_stay_lite(self, monkeypatch):
        from kiln import server

        asked: list[str | None] = []

        def fake_status(printer_name=None, detail=None):
            asked.append(detail)
            answer = {"success": True, "printer": {"state": "printing"}, "job": {}}
            if detail == "full":
                answer["capabilities"] = {"can_pause": True}
            return answer

        monkeypatch.setattr(local_monitor, "_direct_status", self._real_direct_status)
        monkeypatch.setattr(server, "printer_status", fake_status)

        plain = local_monitor.compose_local_payload()
        with_caps = local_monitor.compose_local_payload(include_capabilities=True)

        assert asked == ["lite", "full"]
        assert "capabilities" not in plain["status"]
        assert with_caps["status"]["capabilities"] == {"can_pause": True}

    def test_the_poll_verb_carries_the_ask_through_the_sdk(self, monkeypatch):
        """The door the panel actually polls.  The SDK drops an argument a
        tool does not declare, silently -- which is how the ask went
        unanswered -- so the flag goes in through the registered verb, the
        way a host's ``tools/call`` arrives, not straight to the composer."""
        from kiln import server

        asked: list[str | None] = []

        def fake_status(printer_name=None, detail=None):
            asked.append(detail)
            return {"success": True, "printer": {"state": "printing"}, "job": {}}

        monkeypatch.setattr(local_monitor, "_direct_status", self._real_direct_status)
        monkeypatch.setattr(server, "printer_status", fake_status)
        mcp = _fastmcp()
        assert local_monitor._register_snapshot_verb(mcp)

        def _in_process_client():
            """SDK 2 connects ``Client`` to a server object directly; 1.x has
            the memory-stream helper that 2.x removed."""
            try:
                from mcp import Client
            except ImportError:
                from mcp.shared.memory import create_connected_server_and_client_session

                return create_connected_server_and_client_session(mcp)
            return Client(mcp)

        async def _two_polls() -> None:
            async with _in_process_client() as client:
                await client.call_tool("kiln_monitor_snapshot", {"include_capabilities": True})
                await client.call_tool("kiln_monitor_snapshot", {})

        anyio.run(_two_polls)

        assert asked == ["full", "lite"]


class TestTheStatusRefusalIsUnwrappedFromTheRealShape:
    """The failure axis, driven through the REAL ``_direct_status`` against
    ``printer_status``'s REAL refusal shape.

    The composition test above stubs ``_direct_status`` wholesale, which is
    precisely how the nesting bug reached a live machine: ``_error_dict``
    returns ``{"success": false, "error": {code, message, retryable}}``,
    the door read ``error`` as a string, and the panel — which tells "no
    printer configured" from "printer offline" by reading ``code`` and
    ``message`` — got a dict and a fallback word.  A user with no printer
    set up was shown the remedy for an unplugged one.
    """

    #: Captured before the autouse stub replaces the module attribute.
    _real_direct_status = staticmethod(local_monitor._direct_status)

    def _refuse(self, monkeypatch, answer):
        import kiln.server as server

        monkeypatch.setattr(
            server, "printer_status", lambda printer_name=None, detail=None: answer
        )
        return self._real_direct_status(None)

    def test_the_nested_error_dict_unwraps_to_code_and_sentence(self, monkeypatch):
        """Built with the server's OWN ``_error_dict``, so a change to that
        shape breaks this loudly instead of silently re-nesting."""
        from kiln.server import _error_dict

        answer = _error_dict(
            "Failed to get printer status: No printer configured. Set "
            "KILN_PRINTER_HOST environment variable to the printer URL.",
            code="ERROR",
        )
        status, failure = self._refuse(monkeypatch, answer)

        assert status is None
        assert failure["code"] == "ERROR", "the real code must survive, not a fallback"
        assert isinstance(failure["message"], str), (
            "a dict here renders as '[object Object]' in the panel"
        )
        # The panel's own no-printer test is a substring check on this
        # sentence; pin that it can still match.
        assert "no printer configured" in failure["message"].lower()

    def test_a_named_printer_refusal_carries_its_own_code(self, monkeypatch):
        from kiln.server import _error_dict

        _, failure = self._refuse(
            monkeypatch, _error_dict("Printer 'x' not found.", code="NOT_FOUND")
        )
        assert failure == {"code": "NOT_FOUND", "message": "Printer 'x' not found."}

    def test_a_flat_refusal_still_reads(self, monkeypatch):
        """Any other caller's flat shape must not regress into the fallback."""
        _, failure = self._refuse(
            monkeypatch,
            {"success": False, "code": "PRINTER_NOT_FOUND", "error": "Nope."},
        )
        assert failure == {"code": "PRINTER_NOT_FOUND", "message": "Nope."}

    def test_a_shapeless_refusal_falls_back_without_crashing(self, monkeypatch):
        _, failure = self._refuse(monkeypatch, {"success": False})
        assert failure["code"] == "TOOL_FAILURE"
        assert isinstance(failure["message"], str)

    def test_a_success_answer_is_the_status_axis(self, monkeypatch):
        answer = {"success": True, "printer": {"state": "idle", "connected": True}}
        status, failure = self._refuse(monkeypatch, answer)
        assert failure is None and status is answer


class TestTheRoomCameraRule:
    #: The real ``_camera_frame``, captured before the autouse stub replaces
    #: the module attribute, so the gate itself stays testable.
    _real_camera_frame = staticmethod(local_monitor._camera_frame)

    def test_no_frame_while_no_print_is_active(self):
        """The active-print gate lives server-side: an idle status answers
        with a note, and no adapter is ever asked for photons."""
        frame, note = self._real_camera_frame(
            None, {"printer": {"state": "idle"}}
        )
        assert frame is None
        assert note == "camera is off while no print is active"

    def test_the_state_words_match_the_wire_homes_list(self):
        assert monitor_payload.is_active_print_state("printing") is True
        assert monitor_payload.is_active_print_state("paused") is True
        assert monitor_payload.is_active_print_state("idle") is False
        assert monitor_payload.is_active_print_state(None) is False


def _run_hook(host, tool_name, arguments=None):
    """Drive the real lowlevel hook over a monitor result and return the
    result's structuredContent — the stage harness, pointed at this door."""
    from kiln.mcp_compat import MCP_SDK_MAJOR, lowlevel_server

    _cache_the_monitor()
    mcp = _fastmcp()

    class _Block:
        def __init__(self, text):
            self.text = text
            self.type = "text"

    class _Result:
        def __init__(self):
            self.content = [_Block("PRINTING 41%")]
            self.isError = False
            self.structuredContent = None

    result = _Result()
    server = lowlevel_server(mcp)

    params = None
    if tool_name is not None:
        from mcp.types import CallToolRequestParams

        params = CallToolRequestParams(name=tool_name, arguments=arguments or {})

    if MCP_SDK_MAJOR >= 2:
        entry = server.get_request_handler("tools/call")

        async def _base(_ctx, _params):
            return result

        server.add_request_handler("tools/call", entry.params_type, _base)
        local_monitor.install(mcp)
        handler = server.get_request_handler("tools/call").handler
        anyio.run(handler, host._mcp_server.request_context, params)
        return result.structuredContent

    from mcp.server.lowlevel.server import request_ctx
    from mcp.types import CallToolRequest

    handlers = server.request_handlers

    async def _base_v1(_req):
        return type("R", (), {"root": result})()

    handlers[CallToolRequest] = _base_v1
    local_monitor.install(mcp)
    req = None
    if params is not None:
        req = CallToolRequest(method="tools/call", params=params)
    token = request_ctx.set(host._mcp_server.request_context)
    try:
        anyio.run(handlers[CallToolRequest], req)
    finally:
        request_ctx.reset(token)
    return result.structuredContent


class TestThePayloadRidesTheResult:
    def test_an_apps_host_gets_the_payload_on_a_monitor_result(self):
        sc = _run_hook(_apps_host(), "monitor_print")
        payload = (sc or {}).get(monitor_payload.MONITOR_STRUCTURED_CONTENT_KEY)
        assert payload and payload["kind"] == monitor_payload.MONITOR_PAYLOAD_KIND

    def test_a_host_that_declared_nothing_gets_no_payload(self):
        sc = _run_hook(_Host(_Caps()), "monitor_print")
        assert not (sc or {}).get(monitor_payload.MONITOR_STRUCTURED_CONTENT_KEY)

    def test_a_non_roster_tool_gets_no_payload(self):
        """The strict gate: this hook does printer I/O, so attaching to
        unrelated tools would poll the machine as a side effect."""
        sc = _run_hook(_apps_host(), "list_materials")
        assert not (sc or {}).get(monitor_payload.MONITOR_STRUCTURED_CONTENT_KEY)

    def test_an_unreadable_name_skips_rather_than_polls(self):
        sc = _run_hook(_apps_host(), None)
        assert not (sc or {}).get(monitor_payload.MONITOR_STRUCTURED_CONTENT_KEY)

    def test_the_named_printer_reaches_the_payload(self):
        sc = _run_hook(
            _apps_host(), "monitor_print", {"printer_name": "workshop-a1"}
        )
        payload = (sc or {}).get(monitor_payload.MONITOR_STRUCTURED_CONTENT_KEY)
        assert payload["printer_name_arg"] == "workshop-a1"


#: A frame the size of the real one: 147,000 JPEG bytes encode to 196,000
#: base64 characters, which is what one A1 frame measured on the wire.
_FRAME = __import__("base64").b64encode(b"\xff" * 147_000).decode("ascii")


def _walk_strings(node, path="result"):
    """Every string in a nested result, with the path that reaches it —
    blind to key names, because a buffer is no cheaper for being renamed."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk_strings(value, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


class TestTheResultIsLean:
    """A monitor result never carries the camera frame.  The panel fetches it.

    THE INCIDENT.  2026-09-16, Claude Code desktop, a live A1 print:
    ``monitor_print`` returned a 203,140-character result, 196,592 of them
    one base64 frame under ``kiln_monitor.camera.image_base64``.  The client
    refused the whole result ("exceeds maximum allowed tokens"), wrote it to
    a file, and no panel rendered — the frame cost the text report AND the
    panel it was meant to feed.  The stage retired the same mistake for
    geometry on 2026-08-30 (``TestTheResultIsLean`` next door); this is the
    monitor's copy of that decision, recorded as a test so it cannot be
    reversed by accident.
    """

    #: Nothing in a lean result should be anywhere near this.  The status
    #: dict is a few kilobytes of short strings; the frame is ~200k in one.
    MAX_VALUE_BYTES = 8192

    @pytest.fixture(autouse=True)
    def _a_printing_machine_with_a_camera(self, monkeypatch):
        monkeypatch.setattr(
            local_monitor, "_camera_frame", lambda pn, status: (_FRAME, None)
        )

    def _payload(self):
        sc = _run_hook(_apps_host(), "monitor_print")
        return sc[monitor_payload.MONITOR_STRUCTURED_CONTENT_KEY]

    def test_the_default_is_lean(self, monkeypatch):
        monkeypatch.delenv(local_monitor._INLINE_CAMERA_ENV, raising=False)
        payload = self._payload()
        assert payload["status"]["printer"]["state"] == "printing", (
            "lean still carries the readings — they are what the panel paints first"
        )
        assert "camera" not in payload, (
            "a frame rode a result again; the panel fetches it itself"
        )

    def test_the_lean_result_says_where_the_frame_is(self, monkeypatch):
        """The agent reads structuredContent: a withheld frame must say it
        was withheld, and where the picture is, rather than look like a
        printer with no camera."""
        monkeypatch.delenv(local_monitor._INLINE_CAMERA_ENV, raising=False)
        note = self._payload().get("camera_note") or ""
        assert "kiln_monitor_snapshot" in note
        assert "Camera" in note

    def test_no_value_in_the_result_is_a_frame_buffer(self, monkeypatch):
        """THE REGRESSION GATE — anchored on SIZE, not on a key name."""
        monkeypatch.delenv(local_monitor._INLINE_CAMERA_ENV, raising=False)
        sc = _run_hook(_apps_host(), "monitor_print")
        for path, value in _walk_strings(sc):
            assert len(value) <= self.MAX_VALUE_BYTES, (
                f"{path} carries {len(value)} bytes into the model's context — "
                f"a tool result is not a camera transport"
            )

    def test_the_opt_in_restores_the_inline_frame(self, monkeypatch):
        """The escape hatch is real, and it proves the gate above can fail."""
        monkeypatch.setenv(local_monitor._INLINE_CAMERA_ENV, "1")
        payload = self._payload()
        assert payload["camera"] == {"image_base64": _FRAME}
        assert any(len(v) > self.MAX_VALUE_BYTES for _, v in _walk_strings(payload))

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "  "])
    def test_the_old_off_spellings_still_read_as_lean(self, value, monkeypatch):
        """An install carrying the old ``=0`` must not be surprised into the
        inline path by the inversion."""
        monkeypatch.setenv(local_monitor._INLINE_CAMERA_ENV, value)
        assert "camera" not in self._payload()


class TestThePollVerbStandsFromInstall:
    """With the lean result, ``kiln_monitor_snapshot`` is the panel's ONLY
    route to a frame — and a host that caches its tool list at initialize
    and ignores ``list_changed`` (measured: the Claude desktop host,
    2026-09-01, on the stage's fetch verb) can only call what stood at the
    start.  Registering it at the first resource read, as this door did,
    left the panel polling a verb the host had never heard of."""

    def test_the_verb_stands_from_install(self):
        _cache_the_monitor()
        mcp = _fastmcp()
        out = local_monitor.install(mcp)
        assert out["snapshot_tool"] is True
        assert "kiln_monitor_snapshot" in mcp._tool_manager._tools

    def test_it_stands_even_with_a_cold_cache(self):
        """A server that boots before the panel document is cached still
        answers the panel's first poll once the document lands."""
        mcp = _fastmcp()
        local_monitor.install(mcp)
        assert "kiln_monitor_snapshot" in mcp._tool_manager._tools

    def test_the_verb_is_marked_app_only(self):
        """Standing, but not for the model: hosts that honour the MCP Apps
        visibility hint hide it, so the cost is one row on the wire."""
        _cache_the_monitor()
        mcp = _fastmcp()
        local_monitor.install(mcp)
        tool = mcp._tool_manager._tools["kiln_monitor_snapshot"]
        ui = (getattr(tool, "meta", None) or {}).get("ui") or {}
        assert ui.get("visibility") == ["app"]
        assert ui.get("resourceUri") == local_monitor.PRINT_MONITOR_RESOURCE_URI

    def test_a_second_install_does_not_register_it_twice(self):
        _cache_the_monitor()
        mcp = _fastmcp()
        local_monitor.install(mcp)
        first = mcp._tool_manager._tools["kiln_monitor_snapshot"]
        local_monitor.install(mcp)
        assert mcp._tool_manager._tools["kiln_monitor_snapshot"] is first


class TestTheControlVerbStandsFromInstall:
    """The panel's Pause, Resume and Stop call ``kiln_monitor_control`` by
    name wherever the panel is served.  Only the hosted door had it, so a
    locally served panel's buttons failed with "Couldn't reach your
    printer".  The local verb rides the public tools that already own each
    control, with their own posture, in the shape the panel parses."""

    def _call(self, mcp, args):
        return anyio.run(mcp._tool_manager.call_tool, "kiln_monitor_control", args)

    def test_the_verb_stands_from_install_and_is_app_only(self):
        _cache_the_monitor()
        mcp = _fastmcp()
        out = local_monitor.install(mcp)
        assert out["control_tool"] is True
        tool = mcp._tool_manager._tools["kiln_monitor_control"]
        ui = (getattr(tool, "meta", None) or {}).get("ui") or {}
        assert ui.get("visibility") == ["app"]
        assert ui.get("resourceUri") == local_monitor.PRINT_MONITOR_RESOURCE_URI
        local_monitor.install(mcp)
        assert mcp._tool_manager._tools["kiln_monitor_control"] is tool

    def test_each_action_rides_its_own_public_tool(self, monkeypatch):
        from kiln import server

        calls = []
        for name in ("pause_print", "resume_print", "cancel_print"):
            monkeypatch.setattr(server, name, lambda _n=name, **kw: calls.append((_n, kw)) or {"success": True})
        mcp = _fastmcp()
        assert local_monitor._register_control_verb(mcp)
        for action, tool in local_monitor.MONITOR_CONTROL_ACTIONS.items():
            out = _unwrap(self._call(mcp, {"action": action, "printer_name": "workshop-a1"}))
            assert out == {"kiln_monitor_control": {"status": "accepted", "action": action}}, action
            assert calls[-1] == (tool, {"printer_name": "workshop-a1"})
        # An unnamed call names no printer, the way the tools themselves default.
        _unwrap(self._call(mcp, {"action": "pause"}))
        assert calls[-1] == ("pause_print", {})

    def test_a_refusal_keeps_the_tools_own_words(self, monkeypatch):
        from kiln import server

        monkeypatch.setattr(server, "pause_print", lambda **kw: {
            "success": False, "error": {"code": "NO_ACTIVE_JOB", "message": "No print is running."}})
        monkeypatch.setattr(server, "cancel_print", lambda **kw: (_ for _ in ()).throw(RuntimeError("adapter gone")))
        mcp = _fastmcp()
        assert local_monitor._register_control_verb(mcp)
        out = _unwrap(self._call(mcp, {"action": "pause"}))["kiln_monitor_control"]
        assert out["status"] == "refused" and out["failure"] == {"code": "NO_ACTIVE_JOB", "message": "No print is running."}
        out = _unwrap(self._call(mcp, {"action": "cancel"}))["kiln_monitor_control"]
        assert out["status"] == "refused" and out["failure"]["code"] == "CONTROL_ERROR"
        assert "adapter gone" in out["failure"]["message"]
        out = _unwrap(self._call(mcp, {"action": "reboot"}))["kiln_monitor_control"]
        assert out["status"] == "refused" and out["failure"]["code"] == "INVALID_ACTION"

    def test_a_confirmation_the_panel_cannot_give_is_a_refusal_not_an_accept(self, monkeypatch):
        """Under KILN_CONFIRM_MODE, cancel_print answers with a token for
        confirm_action instead of cancelling.  The verb read that as
        "accepted": the pill said "Stopping…" over a print that kept going.
        The real gate on the real tool: nothing reaches the adapter, and the
        panel is told to ask the agent."""
        from kiln import server

        reached: list = []

        def _adapter_reached(*a, **k):
            reached.append(a)
            raise AssertionError("the adapter was reached")

        monkeypatch.setattr(server, "_CONFIRM_MODE", True)
        monkeypatch.setattr(server, "_check_rate_limit", lambda name: None)
        monkeypatch.setattr(server, "_resolve_control_target", _adapter_reached)
        mcp = _fastmcp()
        assert local_monitor._register_control_verb(mcp)
        out = _unwrap(self._call(mcp, {"action": "cancel"}))["kiln_monitor_control"]
        assert out["status"] == "refused", out
        assert out["failure"]["code"] == "CONFIRMATION_REQUIRED"
        assert "agent" in out["failure"]["message"]
        assert reached == []
        # A control with no confirmation level keeps its door.
        monkeypatch.setattr(server, "pause_print", lambda **kw: {"success": True})
        assert _unwrap(self._call(mcp, {"action": "pause"}))["kiln_monitor_control"]["status"] == "accepted"


def _unwrap(result):
    """The tool's own dict from whatever the SDK's call_tool hands back."""
    if isinstance(result, dict):
        return result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        return result[1]
    if isinstance(result, list):
        for block in result:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                try:
                    return json.loads(text)
                except ValueError:
                    continue
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc
    raise AssertionError(f"unreadable tool result: {result!r}")


class TestMonitorDocumentCache:
    def test_monitor_document_reads_the_cached_file(self):
        _cache_the_monitor()
        assert stage_cache.monitor_document() == _DOC

    def test_the_two_documents_do_not_cross(self):
        (stage_cache.cache_dir() / "mesh_viewer.html").write_text(
            "stage-doc", encoding="utf-8"
        )
        _cache_the_monitor()
        assert stage_cache.document() == "stage-doc"
        assert stage_cache.monitor_document() == _DOC


class TestARepeatWhileThePanelIsLive:
    """The host opens a panel for every call to a stamped tool, and the
    server cannot keep a second one from appearing -- so it says so in the
    result (the stage's own ``shown`` slot) and that panel draws itself as
    a one-line card.  Measured 2026-09-24: three status checks in one
    chat, three identical live panels.  The panel's own poll is its proof
    of life; the person's ask (``show_panel=True``) opens it again."""

    def _poll(self, printer_name=None):
        mcp = _fastmcp()
        assert local_monitor._register_snapshot_verb(mcp)
        args = {"printer_name": printer_name} if printer_name else {}
        anyio.run(mcp._tool_manager.call_tool, "kiln_monitor_snapshot", args)

    def test_the_first_call_opens_a_panel_and_nothing_says_repeat(self):
        sc = _run_hook(_apps_host(), "monitor_print")
        assert sc.get(monitor_payload.MONITOR_STRUCTURED_CONTENT_KEY)
        assert "shown" not in sc

    def test_a_call_while_the_panel_polls_is_said_to_be_a_repeat(self):
        self._poll()
        sc = _run_hook(_apps_host(), "monitor_print")
        assert sc["shown"] == {"repeat": "live_panel", "repeat_note": local_monitor.LIVE_PANEL_NOTE}
        # Said, never suppressed: the readings still ride for the agent.
        payload = sc[monitor_payload.MONITOR_STRUCTURED_CONTENT_KEY]
        assert payload["kind"] == monitor_payload.MONITOR_PAYLOAD_KIND

    def test_the_persons_ask_opens_the_panel_again(self):
        self._poll()
        sc = _run_hook(_apps_host(), "monitor_print", {"show_panel": True})
        assert "shown" not in sc

    def test_a_panel_that_stopped_polling_is_forgotten(self, monkeypatch):
        self._poll()
        later = local_monitor.time.monotonic() + local_monitor.PANEL_LIVE_WINDOW_SECONDS + 1
        monkeypatch.setattr(local_monitor.time, "monotonic", lambda: later)
        sc = _run_hook(_apps_host(), "monitor_print")
        assert "shown" not in sc

    def test_another_printers_panel_does_not_count(self):
        self._poll("workshop-a1")
        assert "shown" not in _run_hook(_apps_host(), "monitor_print")
        sc = _run_hook(_apps_host(), "monitor_print", {"printer_name": "workshop-a1"})
        assert sc["shown"]["repeat"] == "live_panel"

    def test_the_default_printer_is_one_panel_however_the_call_spells_it(self):
        """``monitor_print()`` and ``monitor_print(printer_name="default")``
        are one printer; keyed by the raw argument they were two, and the
        second spelling drew a second live panel."""
        self._poll()
        sc = _run_hook(_apps_host(), "monitor_print", {"printer_name": "default"})
        assert sc["shown"]["repeat"] == "live_panel"
        local_monitor._reset_for_tests()
        self._poll("default")
        assert _run_hook(_apps_host(), "monitor_print")["shown"]["repeat"] == "live_panel"

    def test_the_vision_door_follows_the_same_rule(self):
        self._poll()
        assert _run_hook(_apps_host(), "monitor_print_vision")["shown"]["repeat"] == "live_panel"

    def test_the_note_names_the_doors_that_replace_a_repeat(self):
        note = local_monitor.LIVE_PANEL_NOTE
        assert "printer_status" in note and "first_layer_status" in note
        assert "show_panel=True" in note

    def test_the_clause_says_the_panel_polls_itself(self):
        """The old clause told agents the panel refreshes with each call, so
        keep watching through this tool -- the exact guidance that littered a
        chat with panels."""
        clause = local_monitor.MONITOR_DESCRIPTION_CLAUSE
        assert "polls the printer itself" in clause
        assert "printer_status" in clause and "first_layer_status" in clause
        assert "show_panel=True" in clause
        assert "refreshes with each monitoring call" not in clause
        assert "keep watching through" not in clause

    def test_every_monitor_door_declares_show_panel_so_the_sdk_passes_it(self):
        """The SDK drops an argument a tool does not declare, silently (the
        include_capabilities lesson), so the person's ask must be a declared
        parameter on every door the hook reads."""
        import inspect

        from kiln import server
        from kiln.plugins.monitoring_tools import _MonitoringToolsPlugin

        assert inspect.signature(server.monitor_print).parameters["show_panel"].default is False

        from kiln.mcp_compat import FastMCP

        mcp = FastMCP("plugin")
        _MonitoringToolsPlugin().register(mcp)
        vision = mcp._tool_manager._tools["monitor_print_vision"]
        assert inspect.signature(vision.fn).parameters["show_panel"].default is False
