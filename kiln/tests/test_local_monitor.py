"""Kiln's inline print monitor, served by a local install.

The properties worth defending mirror the stage's, plus this door's own:

* it needs nothing but public Kiln;
* it is ON without anyone setting a flag;
* the payload rides the result ONLY for a host that renders panels, and
  ONLY on roster tools — this hook does printer I/O, so an unknown tool
  must skip, the OPPOSITE of the stage's fail-open rule;
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
    monkeypatch.delenv(local_monitor._DIAGNOSTICS_ENV, raising=False)
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
        lambda printer_name: (
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
        assert local_monitor.install(object()) == {
            "enabled": False, "resource": False, "stamped": 0
        }

    def test_the_poll_verb_stays_off_the_standing_tool_surface(self):
        _cache_the_monitor()
        mcp = _fastmcp()
        local_monitor.install(mcp)
        assert "kiln_monitor_snapshot" not in mcp._tool_manager._tools

    def test_diagnostics_flag_brings_it_back(self, monkeypatch):
        """The stdio-callback re-measure switch: with the verb on the
        surface, a panel that CAN call back will show up in the RPC log."""
        _cache_the_monitor()
        monkeypatch.setenv(local_monitor._DIAGNOSTICS_ENV, "1")
        mcp = _fastmcp()
        local_monitor.install(mcp)
        tool = mcp._tool_manager._tools.get("kiln_monitor_snapshot")
        assert tool is not None
        ui = (tool.meta or {}).get("ui") or {}
        assert ui.get("visibility") == ["app"]
        assert ui.get("resourceUri") == local_monitor.PRINT_MONITOR_RESOURCE_URI


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

    def test_the_read_registers_the_poll_verb_for_door_parity(self):
        """A host that renders the panel may try to poll; the verb must
        exist before the View's first tools/call can miss it."""
        _cache_the_monitor()
        mcp = _fastmcp()
        local_monitor.install(mcp)
        assert "kiln_monitor_snapshot" not in mcp._tool_manager._tools
        anyio.run(mcp.read_resource, local_monitor.PRINT_MONITOR_RESOURCE_URI)
        assert "kiln_monitor_snapshot" in mcp._tool_manager._tools

    def test_a_cold_cache_read_raises_and_registers_nothing(self):
        mcp = _fastmcp()
        local_monitor.install(mcp)
        with pytest.raises(Exception):
            anyio.run(mcp.read_resource, local_monitor.PRINT_MONITOR_RESOURCE_URI)
        assert "kiln_monitor_snapshot" not in mcp._tool_manager._tools


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
            lambda printer_name: (
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
