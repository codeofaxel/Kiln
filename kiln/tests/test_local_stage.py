"""Kiln's inline 3D stage, served by a local install.

The three properties worth defending:

* it needs nothing but public Kiln — the whole point of the module;
* it is ON without anyone setting a flag, because a flag deciding whether
  a user can turn their own part over is a two-tier experience;
* the geometry rides the result ONLY for a host that renders the panel.
  ~1.9 MB of base64 handed to a host that will never draw it goes straight
  into the model's context and buys nothing.
"""

from __future__ import annotations

import json
import struct
import sys

import pytest

from kiln import local_stage, stage_cache

_DOC = "<!DOCTYPE html><html><body>stage</body></html>"


def _stl(path, triangles: int = 2):
    data = bytearray(b"\x00" * 80) + struct.pack("<I", triangles)
    data += b"\x00" * (50 * triangles)
    path.write_bytes(bytes(data))
    return str(path)


def _real_cube(path):
    """A cube with actual geometry — the encoder needs something to encode."""
    trimesh = pytest.importorskip("trimesh")
    trimesh.creation.box(extents=(20.0, 20.0, 20.0)).export(str(path))
    return str(path)


def _real_mesh_worth_truncating(path):
    """A mesh whose payload is big enough to be the bug.

    A 12-triangle cube encodes to a few hundred bytes, which cannot
    demonstrate a size gate in either direction — measured, when the
    opt-in half of ``TestTheResultIsLean`` first asserted the gate could
    fire and could not.  This is a real make's order of magnitude: an
    icosphere at ~5k triangles encodes past every threshold in that class,
    and it is what the incident's own recipe produced at its LOWEST
    resolution.
    """
    trimesh = pytest.importorskip("trimesh")
    trimesh.creation.icosphere(subdivisions=4, radius=10.0).export(str(path))
    return str(path)


class _Block:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, payload, isError=False, structuredContent=None):
        self.content = [_Block(json.dumps(payload))] if payload is not None else []
        self.isError = isError
        self.structuredContent = structuredContent


class _Caps:
    """A host's declared capabilities, the shape the SDK hands back."""

    def __init__(self, extensions=None, experimental=None):
        self.experimental = experimental
        self.model_extra = {"extensions": extensions} if extensions is not None else {}


class _Host:
    """Just enough of a FastMCP to answer "what did the client declare?"."""

    def __init__(self, caps=None, name="TestHost"):
        session = type("S", (), {})()
        session.client_params = type("P", (), {})()
        session.client_params.capabilities = caps
        session.client_params.clientInfo = type("I", (), {"name": name, "version": "1"})()
        ctx = type("C", (), {})()
        ctx.session = session
        self._mcp_server = type("L", (), {"request_context": ctx})()


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln_home"))
    monkeypatch.delenv(local_stage._OPT_OUT_ENV, raising=False)
    monkeypatch.delenv(local_stage._DIAGNOSTICS_ENV, raising=False)
    local_stage._reset_for_tests()
    stage_cache._reset_for_tests()
    yield
    local_stage._reset_for_tests()
    stage_cache._reset_for_tests()


def _cache_the_stage():
    (stage_cache.cache_dir() / "mesh_viewer.html").write_text(_DOC, encoding="utf-8")
    stage_cache._reset_for_tests()


class TestNeedsNothingButPublicKiln:
    def test_the_module_does_not_reach_into_kiln_pro(self):
        """The reason this module exists: a FREE local install gets the stage."""
        src = (
            __import__("pathlib").Path(local_stage.__file__).read_text(encoding="utf-8")
        )
        assert "kiln_pro" not in src

    def test_it_installs_a_working_stage_with_kiln_pro_unimportable(self, tmp_path):
        """In a subprocess, because the only honest way to prove kiln-pro is
        absent is to make it genuinely unimportable."""
        import subprocess
        import textwrap

        prog = textwrap.dedent(
            """
            import sys
            class _Block:
                def find_module(self, name, path=None):
                    if name == "kiln_pro" or name.startswith("kiln_pro."):
                        raise ImportError("kiln_pro is not installed")
            sys.meta_path.insert(0, _Block())
            from kiln.mcp_compat import FastMCP
            from kiln import local_stage, stage_cache
            (stage_cache.cache_dir() / "mesh_viewer.html").write_text("<!DOCTYPE html>x")
            mcp = FastMCP("t")
            @mcp.tool(name="compile_scad")
            def compile_scad() -> dict: return {"success": True}
            out = local_stage.install(mcp)
            assert out["resource"] and out["stamped"] == 1, out
            print("OK")
            """
        )
        import os

        env = {
            **os.environ,
            "KILN_HOME": str(tmp_path),
            # Hand the child this interpreter's search path verbatim; it is
            # not inheriting a virtualenv or a user site-dir by itself.
            "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
        }
        r = subprocess.run([sys.executable, "-c", prog], capture_output=True,
                           text=True, env=env, timeout=180)
        assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-2000:]


class TestOnByDefault:
    def test_enabled_with_no_flag_set(self):
        assert local_stage.enabled() is True

    def test_opt_out_turns_everything_off(self, monkeypatch, tmp_path):
        monkeypatch.setenv(local_stage._OPT_OUT_ENV, "1")
        assert local_stage.enabled() is False
        assert local_stage.install(object()) == {  # would explode if it did anything
            "enabled": False, "resource": False, "payload_tool": False, "stamped": 0}
        assert local_stage.token_for_call_result(
            _Result({"success": True, "stl_path": _stl(tmp_path / "a.stl")})) is None

    def test_the_support_verbs_stay_off_the_standing_tool_surface(self):
        """Neither is useful to a person or an agent; a tool nobody should
        call is a permanent tax on everyone reading the tool list."""
        _cache_the_stage()
        mcp = _fastmcp()
        local_stage.install(mcp)
        assert "kiln_viewer_payload" not in mcp._tool_manager._tools
        assert "stage_smoke_test" not in mcp._tool_manager._tools

    def test_diagnostics_flag_brings_them_back(self, monkeypatch):
        _cache_the_stage()
        monkeypatch.setenv(local_stage._DIAGNOSTICS_ENV, "1")
        mcp = _fastmcp()
        local_stage.install(mcp)
        assert "kiln_viewer_payload" in mcp._tool_manager._tools
        assert "stage_smoke_test" in mcp._tool_manager._tools


def _fastmcp():
    from kiln.mcp_compat import FastMCP

    mcp = FastMCP("test")

    @mcp.tool(name="compile_scad")
    def compile_scad() -> dict:
        """Compile OpenSCAD source into a mesh."""
        return {"success": True}

    @mcp.tool(name="list_materials")
    def list_materials() -> dict:
        return {"success": True}

    return mcp


class TestInstallOnARealFastMCP:
    """Against a real FastMCP, since that is what `kiln serve` runs."""

    def test_registers_the_resource_and_stamps_only_mesh_tools(self):
        _cache_the_stage()
        mcp = _fastmcp()
        out = local_stage.install(mcp)
        assert out["resource"] and out["token_hook"] and out["stamped"] == 1
        tools = mcp._tool_manager._tools
        assert (tools["compile_scad"].meta or {})["ui"]["resourceUri"] == (
            local_stage.MESH_VIEWER_RESOURCE_URI
        )
        assert not (tools["list_materials"].meta or {}).get("ui"), (
            "stamped a tool that produces no mesh — every call would try to "
            "open a 3D panel"
        )

    def test_install_is_idempotent(self):
        _cache_the_stage()
        mcp = _fastmcp()
        local_stage.install(mcp)
        assert local_stage.install(mcp)["resource"], (
            "a second install must not break the first"
        )

    def test_stamped_tools_say_so_in_their_descriptions(self):
        """The _meta stamp is host-facing; agents read DESCRIPTIONS.  A
        2026-08-02 session keyword-searched the tool surface for the
        interactive viewer, found nothing (the stage lived only in _meta and
        the result hook), and shipped PNGs to a user who asked for the
        stage.  The clause is the searchable surface of the capability."""
        _cache_the_stage()
        mcp = _fastmcp()
        local_stage.install(mcp)
        tools = mcp._tool_manager._tools
        desc = tools["compile_scad"].description or ""
        assert local_stage.STAGE_DESCRIPTION_CLAUSE in desc
        # The words an agent would actually search with must be present.
        for keyword in ("3D stage", "interactive", "inline", "orbit"):
            assert keyword in desc, f"stage clause not findable by {keyword!r}"
        # And the original docstring survives in front of it.
        assert desc.index(local_stage.STAGE_DESCRIPTION_CLAUSE) > 0

    def test_non_stage_tools_do_not_claim_a_panel(self):
        _cache_the_stage()
        mcp = _fastmcp()
        local_stage.install(mcp)
        desc = mcp._tool_manager._tools["list_materials"].description or ""
        assert local_stage.STAGE_DESCRIPTION_CLAUSE not in desc, (
            "a tool that opens no panel promising one teaches agents the "
            "clause is noise"
        )

    def test_a_second_install_does_not_stutter_the_clause(self):
        _cache_the_stage()
        mcp = _fastmcp()
        local_stage.install(mcp)
        local_stage.install(mcp)
        desc = mcp._tool_manager._tools["compile_scad"].description or ""
        assert desc.count(local_stage.STAGE_DESCRIPTION_CLAUSE) == 1

    def test_a_cold_cache_still_installs(self):
        """No document downloaded yet is not a reason to break the server —
        the resource simply has nothing to serve until the warm lands."""
        mcp = _fastmcp()
        out = local_stage.install(mcp)
        assert out["resource"] and out["stamped"] == 1


class TestTheStageDocumentComesFromTheCache:
    def test_reading_the_resource_serves_the_cached_document(self):
        import anyio

        _cache_the_stage()
        mcp = _fastmcp()
        local_stage.install(mcp)
        got = anyio.run(mcp.read_resource, local_stage.MESH_VIEWER_RESOURCE_URI)
        assert list(got)[0].content == _DOC

    def test_reading_the_resource_proves_the_host_renders_panels(self):
        """No host reads a ui:// document unless it is about to draw it."""
        import anyio

        _cache_the_stage()
        mcp = _fastmcp()
        local_stage.install(mcp)
        blank = _Host(caps=None)
        assert local_stage.host_renders_apps(blank) is False
        anyio.run(mcp.read_resource, local_stage.MESH_VIEWER_RESOURCE_URI)
        assert local_stage.host_renders_apps(blank) is True

    def test_a_rendering_host_gets_the_payload_verb_with_the_stage(self, tmp_path):
        """Door parity, the widget's return path: a stamped declaration
        promises the rendered View a working ``kiln_viewer_payload`` on
        THIS door.  The verb stays off the standing tool surface until a
        host proves it renders panels (the stage read) — and from that
        moment, which precedes the View's first ``tools/call``, the View
        can never call into a missing verb.  (2026-08-06: a token-only
        lean result plus an unregistered verb starves the panel into the
        viewer's "3D view never arrived" card.)
        """
        import anyio

        _cache_the_stage()
        mcp = _fastmcp()
        local_stage.install(mcp)
        assert "kiln_viewer_payload" not in mcp._tool_manager._tools

        anyio.run(mcp.read_resource, local_stage.MESH_VIEWER_RESOURCE_URI)
        assert "kiln_viewer_payload" in mcp._tool_manager._tools

        # Idempotent across repeat reads — one verb, no duplicate warning.
        anyio.run(mcp.read_resource, local_stage.MESH_VIEWER_RESOURCE_URI)
        assert list(mcp._tool_manager._tools).count("kiln_viewer_payload") == 1

        # And it genuinely serves: a minted token round-trips to the
        # kiln.mesh.v1 payload; a bogus token gets the honest refusal.
        fn = mcp._tool_manager._tools["kiln_viewer_payload"].fn
        token = local_stage._mint(_real_cube(tmp_path / "cube.stl"))
        served = fn(artifact_token=token)
        payload = served[local_stage.VIEWER_STRUCTURED_CONTENT_KEY]
        assert payload["kind"] == "kiln.mesh.v1"
        assert fn(artifact_token="no-such-token")["success"] is False

    def test_a_cold_cache_refuses_rather_than_serving_an_empty_stage(self):
        import anyio

        mcp = _fastmcp()
        local_stage.install(mcp)
        with pytest.raises(Exception):
            anyio.run(mcp.read_resource, local_stage.MESH_VIEWER_RESOURCE_URI)


class TestOnlyAnAppsHostGetsTheGeometry:
    UI = local_stage.MCP_APPS_EXTENSION_ID

    def test_a_host_that_declared_nothing_is_not_given_geometry(self):
        assert local_stage.host_renders_apps(_Host(_Caps())) is False

    def test_a_host_that_declared_the_extension_is(self):
        assert local_stage.host_renders_apps(_Host(_Caps(extensions={self.UI: {}}))) is True

    def test_the_experimental_spelling_counts_too(self):
        """SDKs park unrecognised extensions there; a host that supports MCP
        Apps should not lose the stage over which key it used."""
        assert local_stage.host_renders_apps(
            _Host(_Caps(experimental={self.UI: {}}))) is True

    def test_an_unrelated_extension_does_not(self):
        assert local_stage.host_renders_apps(
            _Host(_Caps(extensions={"io.example/other": {}}))) is False

    def test_no_session_at_all_reads_as_no(self):
        assert local_stage.host_renders_apps(object()) is False

    def test_a_real_claude_desktop_handshake_gets_the_geometry(self):
        """The captured article, parsed by the real SDK model.

        Verbatim from Claude Desktop's own initialize request
        (``~/Library/Logs/Claude/mcp-server-kiln.log``, 2026-07-29) — the
        host this stage was built for.  Everything else in this class is a
        hand-built double, which proves the LOGIC and could agree with
        itself about the wrong wire shape forever; ``extensions`` is not a
        modelled field on ClientCapabilities, so whether it survives parsing
        at all is an SDK behaviour, not ours.  If a future SDK stops
        carrying it, or someone narrows _declared_extensions, this is the
        test that notices — and the failure mode it guards is silent: no
        error, no panel, just a still image where the 3D stage used to be.
        """
        from mcp.types import InitializeRequestParams

        params = InitializeRequestParams.model_validate({
            "protocolVersion": "2025-06-18",
            "capabilities": {"extensions": {
                "io.modelcontextprotocol/ui": {
                    "mimeTypes": ["text/html;profile=mcp-app"]}}},
            "clientInfo": {"name": "claude-ai", "version": "0.1.0"},
        })

        class _Real:
            def __init__(self):
                session = type("S", (), {})()
                session.client_params = params
                ctx = type("C", (), {})()
                ctx.session = session
                self._mcp_server = type("L", (), {"request_context": ctx})()

        host = _Real()
        assert local_stage.host_renders_apps(host) is True, (
            "the host this was built for would get a still image"
        )
        declared = local_stage._declared_extensions(host)
        assert local_stage.MCP_APP_MIME_TYPE in (
            declared[self.UI]["mimeTypes"]
        ), "the host asked for a mimetype the stage does not serve"

    def test_the_payload_is_withheld_from_a_silent_host(self, tmp_path):
        sc = _run_hook(_Host(_Caps()), _real_cube(tmp_path / "cube.stl"))
        assert sc["artifact"]["artifact_token"], "the token is cheap and always rides"
        assert "kiln_viewer" not in sc, (
            "handed ~MBs of base64 to a host that will never draw it"
        )

    def test_the_payload_rides_for_an_apps_host_that_opted_in(
        self, tmp_path, monkeypatch
    ):
        """The host gate still decides the geometry — under the opt-in.

        Inline geometry is off by default now (see ``TestTheResultIsLean``),
        so this asserts the gate it was written for: with the operator's
        opt-in set, an Apps host is the one that gets the payload.
        """
        monkeypatch.setenv("KILN_STAGE_INLINE_GEOMETRY", "1")
        sc = _run_hook(_Host(_Caps(extensions={self.UI: {}})), _real_cube(tmp_path / "c.stl"))
        assert sc["kiln_viewer"]["kind"] == "kiln.mesh.v1"
        assert sc["kiln_viewer"]["counts"]["triangles"] == 12

    def test_the_payload_is_withheld_from_an_apps_host_by_default(self, tmp_path):
        """No opt-in, no geometry — even from the host that would draw it.

        This is the 2026-08-30 incident in one line: an Apps host is
        exactly the host that also feeds structuredContent to the model, so
        "it renders the panel" is not a licence to spend the agent's
        context and truncate the tool's own output.
        """
        sc = _run_hook(_Host(_Caps(extensions={self.UI: {}})), _real_cube(tmp_path / "c.stl"))
        assert sc["artifact"]["artifact_token"], "the token is the lazy fetch's key"
        assert "kiln_viewer" not in sc
        assert sc["success"] is True, "the tool's own output must survive"

    def test_the_tools_own_output_survives_the_hook(self, tmp_path):
        """A host that prefers structuredContent shows THAT and nothing else;
        seeding it with only the token hid success, paths and message."""
        sc = _run_hook(_Host(_Caps()), _real_cube(tmp_path / "c.stl"))
        assert sc["success"] is True
        assert sc["message"] == "made a thing"


def _run_hook(host, mesh_path, tool_name=None, extra=None):
    """Drive the real lowlevel hook over a result and return its
    structuredContent.

    The two majors hand the hook the connected host differently — 1.x
    through a contextvar the dispatcher sets before every handler, 2.x as
    the handler's first argument — so this is one of the few places that
    branches on ``MCP_SDK_MAJOR`` (same shape as
    ``test_mcp_compat_call_tool_wrapper._server_with_base_handler``).

    ``tool_name`` shapes the request the wrapper inspects: named, the hook
    knows which tool ran and applies the per-tool stamp gate; None mimics a
    request shape the name extraction cannot read, which must fail open.
    """
    import anyio

    from kiln.mcp_compat import MCP_SDK_MAJOR, lowlevel_server

    _cache_the_stage()
    mcp = _fastmcp()
    body = {"success": True, "message": "made a thing", "stl_path": mesh_path}
    body.update(extra or {})
    result = _Result(body)
    server = lowlevel_server(mcp)

    params = None
    if tool_name is not None:
        from mcp.types import CallToolRequestParams

        params = CallToolRequestParams(name=tool_name, arguments={})

    if MCP_SDK_MAJOR >= 2:
        entry = server.get_request_handler("tools/call")

        async def _base(_ctx, _params):
            return result

        server.add_request_handler("tools/call", entry.params_type, _base)
        local_stage.install(mcp)
        handler = server.get_request_handler("tools/call").handler
        # The connected host rides in as the request context argument.
        anyio.run(handler, host._mcp_server.request_context, params)
        return result.structuredContent

    from mcp.server.lowlevel.server import request_ctx
    from mcp.types import CallToolRequest

    handlers = server.request_handlers

    async def _base_v1(_req):
        return type("R", (), {"root": result})()

    handlers[CallToolRequest] = _base_v1
    local_stage.install(mcp)
    req = None
    if params is not None:
        req = CallToolRequest(method="tools/call", params=params)
    # The connected host, where the real server reads it from: the
    # context var the lowlevel dispatcher sets before every handler.
    token = request_ctx.set(host._mcp_server.request_context)
    try:
        anyio.run(handlers[CallToolRequest], req)
    finally:
        request_ctx.reset(token)
    return result.structuredContent


class TestTokenMinting:
    def test_mints_for_a_mesh_result_and_resolves_back(self, tmp_path):
        mesh = _stl(tmp_path / "part.stl")
        tok = local_stage.token_for_call_result(_Result({"success": True, "stl_path": mesh}))
        assert tok and local_stage.resolve(tok) == mesh

    def test_token_is_opaque_and_never_a_path(self, tmp_path):
        """It round-trips through the host; a user's disk layout must not."""
        mesh = _stl(tmp_path / "secret_project.stl")
        tok = local_stage.token_for_call_result(_Result({"success": True, "stl_path": mesh}))
        assert "/" not in tok and "secret_project" not in tok

    def test_no_token_for_a_result_without_geometry(self):
        assert local_stage.token_for_call_result(
            _Result({"success": True, "materials": ["PLA"]})) is None

    def test_no_token_for_a_failed_call(self, tmp_path):
        r = _Result({"success": False, "error": "nope",
                     "stl_path": _stl(tmp_path / "a.stl")})
        assert local_stage.token_for_call_result(r) is None

    def test_no_token_when_the_result_is_an_error(self, tmp_path):
        r = _Result({"success": True, "stl_path": _stl(tmp_path / "a.stl")}, isError=True)
        assert local_stage.token_for_call_result(r) is None

    def test_no_token_for_a_path_that_does_not_exist(self):
        assert local_stage.token_for_call_result(
            _Result({"success": True, "stl_path": "/nope/gone.stl"})) is None

    def test_input_mesh_never_becomes_the_staged_one(self, tmp_path):
        """A repair reports both; staging the input would show the break."""
        before, after = _stl(tmp_path / "b.stl"), _stl(tmp_path / "a.stl", 5)
        tok = local_stage.token_for_call_result(
            _Result({"success": True, "input_mesh_path": before, "repaired_stl": after}))
        assert local_stage.resolve(tok) == after

    def test_existing_hosted_token_is_left_alone(self, tmp_path):
        r = _Result({"success": True, "stl_path": _stl(tmp_path / "a.stl")},
                    structuredContent={"artifact": {"artifact_token": "hosted-tok"}})
        assert local_stage.token_for_call_result(r) is None

    def test_prose_content_does_not_raise(self):
        assert local_stage.token_for_call_result(_Result(None)) is None
        r = _Result(None)
        r.content = [_Block("just some prose, not JSON")]
        assert local_stage.token_for_call_result(r) is None

    def test_token_store_is_bounded(self, tmp_path):
        for i in range(local_stage._TOKENS_MAX + 20):
            local_stage.token_for_call_result(
                _Result({"success": True, "stl_path": _stl(tmp_path / f"m{i}.stl", i % 4 + 1)}))
        assert len(local_stage._tokens) <= local_stage._TOKENS_MAX

    def test_never_raises_on_a_junk_result(self):
        class _Junk:
            content = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        assert local_stage.token_for_call_result(_Junk()) is None


class TestStructuredContentPreservesTheToolsOutput:
    def test_result_as_dict_reads_the_tools_own_return(self):
        r = _Result({"success": True, "message": "made a coaster", "stl_path": "/x/a.stl"})
        assert local_stage._result_as_dict(r) == {
            "success": True, "message": "made a coaster", "stl_path": "/x/a.stl"}

    def test_result_as_dict_survives_prose(self):
        r = _Result(None)
        r.content = [_Block("not json at all")]
        assert local_stage._result_as_dict(r) is None


class TestPayloadFollowsTheStamp:
    """The per-tool gate: geometry rides only into a panel that will open.

    A host opens the panel only for tools stamped ``_meta.ui.resourceUri``,
    so geometry attached to an unstamped tool's result is dead weight — a
    slicer echoing the path it just sliced was paying megabytes of base64
    for a panel it cannot have.  The stamp on the registered tool is the
    single decision; every unreadable shape fails OPEN, because a rendered
    panel cannot fetch geometry it was never handed.
    """

    UI = local_stage.MCP_APPS_EXTENSION_ID

    @pytest.fixture(autouse=True)
    def _opted_in(self, monkeypatch):
        """The stamp gate only has an effect where geometry could ride at
        all, so this class exercises it under the operator opt-in.  What the
        DEFAULT does is ``TestTheResultIsLean``'s question, not this one's."""
        monkeypatch.setenv("KILN_STAGE_INLINE_GEOMETRY", "1")

    def _apps_host(self):
        return _Host(_Caps(extensions={self.UI: {}}))

    def test_a_stamped_tool_still_gets_the_geometry(self, tmp_path):
        sc = _run_hook(self._apps_host(), _real_cube(tmp_path / "c.stl"),
                       tool_name="compile_scad")
        assert sc["kiln_viewer"]["kind"] == "kiln.mesh.v1"

    def test_an_unstamped_tool_gets_the_token_but_not_the_geometry(self, tmp_path):
        # list_materials registers in the harness and is NOT on the roster,
        # so install() leaves it unstamped — no panel opens for its results.
        sc = _run_hook(self._apps_host(), _real_cube(tmp_path / "c.stl"),
                       tool_name="list_materials")
        assert sc["artifact"]["artifact_token"], "the token is cheap and always rides"
        assert "kiln_viewer" not in sc, (
            "geometry attached for a tool whose declaration opens no panel"
        )
        assert sc["success"] is True, "the tool's own output must survive the gate"

    def test_an_unreadable_name_fails_open(self, tmp_path):
        # tool_name=None mimics a request shape the name extraction cannot
        # read: the worst case must be yesterday's behavior (attach).
        sc = _run_hook(self._apps_host(), _real_cube(tmp_path / "c.stl"))
        assert sc["kiln_viewer"]["kind"] == "kiln.mesh.v1"

    def test_a_tool_the_registry_does_not_know_fails_open(self, tmp_path):
        sc = _run_hook(self._apps_host(), _real_cube(tmp_path / "c.stl"),
                       tool_name="somebody_elses_tool")
        assert sc["kiln_viewer"]["kind"] == "kiln.mesh.v1"


class TestCadFactsRideTheInlinePayload:
    """A STEP import's analytic truth reaches the stage payload — the local
    half of "no stage shows a STEP as a bare mesh with no facts"."""

    UI = local_stage.MCP_APPS_EXTENSION_ID

    @pytest.fixture(autouse=True)
    def _opted_in(self, monkeypatch):
        """Grades what the payload CONTAINS, which needs a payload — so it
        runs under the operator opt-in.  The same facts reach the panel on
        the default path through ``kiln_viewer_payload``, which builds the
        payload from the identical ``_payload_for_mesh`` door."""
        monkeypatch.setenv("KILN_STAGE_INLINE_GEOMETRY", "1")

    def test_import_facts_reach_the_stage_payload(self, tmp_path):
        facts = {
            "kind": "kiln.step_facts.v1",
            "available": True,
            "source": "read",
            "solids": 1,
            "surfaces": {"plane": 6},
            "cylinders": {"count": 0, "radii_mm": []},
            "header": {"stamped_by_kiln": False, "name": "vendor_part"},
        }
        host = _Host(_Caps(extensions={self.UI: {}}))
        sc = _run_hook(
            host, _real_cube(tmp_path / "c.stl"), extra={"cad_facts": facts}
        )
        payload = sc[local_stage.VIEWER_STRUCTURED_CONTENT_KEY]
        assert payload["cad"]["solids"] == 1
        assert payload["cad"]["header"]["name"] == "vendor_part"
        # The stage names the STEP, not the tessellation it rode in on.
        assert payload["source"]["format"] == "step"

    def test_unavailable_facts_still_ride(self, tmp_path):
        """A kernel-less install's import still tells the stage WHY there
        are no facts — silence would read as "just a mesh"."""
        facts = {
            "kind": "kiln.step_facts.v1",
            "available": False,
            "reason": "no kernel",
            "header": {"stamped_by_kiln": False},
        }
        host = _Host(_Caps(extensions={self.UI: {}}))
        sc = _run_hook(
            host, _real_cube(tmp_path / "c.stl"), extra={"cad_facts": facts}
        )
        payload = sc[local_stage.VIEWER_STRUCTURED_CONTENT_KEY]
        assert payload["cad"]["available"] is False

    def test_a_plain_mesh_result_gains_no_cad_block(self, tmp_path):
        host = _Host(_Caps(extensions={self.UI: {}}))
        sc = _run_hook(host, _real_cube(tmp_path / "c.stl"))
        payload = sc[local_stage.VIEWER_STRUCTURED_CONTENT_KEY]
        assert "cad" not in payload
        assert payload["source"]["format"] == "stl"


class TestTheResultIsLean:
    """A tool result never carries the mesh.  The token does.

    THE INCIDENT.  ``build_organic_mesh`` returned a success payload whose
    ``kiln_viewer`` block held the whole mesh as base64 — ~1 MB for a 20k
    triangle make.  The client surfaced ``structuredContent`` to the model
    as text, so the result blew past the 25k-token output cap and TRUNCATED:
    ``mesh_path``, the fit verdict and the self-check bundle never reached
    the agent that had to act on them.  The panel gained nothing it could
    not have fetched itself.  (Measured 2026-08-19, hit live 2026-08-30.)

    THIS CLASS ASSERTS THE INVERSE OF WHAT IT USED TO.  It replaced
    ``TestInlineGeometryLever``, whose ``test_default_is_inline_geometry``
    guarded the old default with "that is a decision for the live host
    experiment, not a side effect."  It was: the experiment ran, and the
    decision is recorded here rather than in a deleted test, because a
    green test asserting the opposite of the shipped rule is how a decision
    gets reversed by accident.
    """

    UI = local_stage.MCP_APPS_EXTENSION_ID

    #: Nothing in a lean result should be anywhere near this.  A cube's
    #: whole envelope is a few hundred bytes; the payload this guards
    #: against starts in the hundreds of kilobytes.  Sized to catch the
    #: incident with room to spare rather than to bless a smaller leak.
    MAX_VALUE_BYTES = 8192

    def _apps_host(self):
        return _Host(_Caps(extensions={self.UI: {}}))

    def test_the_default_is_lean(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KILN_STAGE_INLINE_GEOMETRY", raising=False)
        sc = _run_hook(self._apps_host(), _real_cube(tmp_path / "c.stl"),
                       tool_name="compile_scad")
        assert sc["artifact"]["artifact_token"], (
            "lean still mints the token — it is the panel's fetch key"
        )
        assert "kiln_viewer" not in sc, (
            "geometry rode a result again; the panel fetches it itself"
        )

    def test_the_tools_own_output_survives_intact(self, tmp_path, monkeypatch):
        """The half of the incident that actually broke the make.

        Truncation is not a cosmetic cost: an agent that cannot read
        ``mesh_path`` back cannot continue the design it just built.
        """
        monkeypatch.delenv("KILN_STAGE_INLINE_GEOMETRY", raising=False)
        mesh = _real_cube(tmp_path / "c.stl")
        sc = _run_hook(self._apps_host(), mesh, tool_name="compile_scad")
        assert sc["success"] is True
        assert sc["message"] == "made a thing"
        assert sc["stl_path"] == mesh

    def test_no_value_in_the_result_is_a_geometry_buffer(
        self, tmp_path, monkeypatch
    ):
        """THE REGRESSION GATE — anchored on SIZE, not on a key name.

        ``kiln_viewer`` is the key that did it once.  The rule is bigger
        than that key: no tool result may carry a base64 buffer, under any
        name, at any nesting depth.  A future door that spells the payload
        differently fails here on the first mesh it ships, which the
        name-anchored check above would sail past.
        """
        monkeypatch.delenv("KILN_STAGE_INLINE_GEOMETRY", raising=False)
        sc = _run_hook(self._apps_host(),
                       _real_mesh_worth_truncating(tmp_path / "s.stl"),
                       tool_name="compile_scad")
        for path, value in _walk_strings(sc):
            assert len(value) <= self.MAX_VALUE_BYTES, (
                f"{path} carries {len(value)} bytes of payload into the "
                f"model's context — a tool result is not a mesh transport"
            )

    def test_the_opt_in_restores_the_inline_payload(self, tmp_path, monkeypatch):
        """The escape hatch is real, and this is what proves the gate above
        can fail: the same call, same mesh, opted in, DOES carry the buffer
        the lean assertions forbid."""
        monkeypatch.setenv("KILN_STAGE_INLINE_GEOMETRY", "1")
        sc = _run_hook(self._apps_host(),
                       _real_mesh_worth_truncating(tmp_path / "s.stl"),
                       tool_name="compile_scad")
        assert sc["kiln_viewer"]["kind"] == "kiln.mesh.v1"
        assert any(
            len(value) > self.MAX_VALUE_BYTES for _, value in _walk_strings(sc)
        ), (
            "the opt-in produced nothing oversized, so the size gate above "
            "was never in a position to catch anything"
        )

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "  "])
    def test_the_old_off_spellings_still_read_as_lean(
        self, value, tmp_path, monkeypatch
    ):
        """An install carrying the old ``=0`` must not be surprised into
        the inline path by the inversion."""
        monkeypatch.setenv("KILN_STAGE_INLINE_GEOMETRY", value)
        sc = _run_hook(self._apps_host(), _real_cube(tmp_path / "c.stl"),
                       tool_name="compile_scad")
        assert "kiln_viewer" not in sc


def _walk_strings(node, path="result"):
    """Every string in a nested result, with the path that reaches it.

    Deliberately blind to key NAMES: the gate above is about how many bytes
    a result hands the model, and a buffer is no cheaper for being called
    something else.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk_strings(value, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


class TestTheHostIsToldTheVerbArrived:
    """Registering the fetch verb late is only half the promise.

    ``kiln_viewer_payload`` is registered at the stage read rather than at
    install, so it stays off the standing tool surface of every host that
    never renders a panel.  Measured on SDK 1.x: FastMCP sends no
    ``notifications/tools/list_changed`` for a tool added after connect —
    so the SERVER can find the verb and a host validating a ``tools/call``
    name against the list it cached at initialize cannot.

    That was survivable while geometry rode the result and this fetch was a
    fallback.  With the lean result it is the ONLY route to a mesh, so a
    host that will not proxy an unannounced name would show "Preview
    unavailable" on every make.  Hence the explicit notification.
    """

    def _session(self, sent, fail=False):
        class _Session:
            async def send_tool_list_changed(_self):
                if fail:
                    raise RuntimeError("host hung up")
                sent.append(True)

        return _Session()

    def _read_with_session(self, mcp, session):
        """Read the stage document with *session* where the server keeps it.

        The majors disagree about where that is, so this asks once which
        dialect it is speaking (same shape as ``_run_hook`` above).

        SDK 1.x parks the request context on a module contextvar the
        lowlevel dispatcher sets before every handler.  SDK 2 removed both
        that contextvar and ``Server.request_context``, and hands the
        ``ServerRequestContext`` to the handler as an argument instead — so
        the read is driven through the real ``resources/read`` handler
        here, which is the only way a session reaches the stage on 2.x and
        therefore the only honest way to test that it does.
        """
        import anyio

        from kiln.mcp_compat import MCP_SDK_MAJOR, lowlevel_server

        if MCP_SDK_MAJOR >= 2:
            from mcp.server.context import ServerRequestContext
            from mcp_types import ReadResourceRequestParams

            entry = lowlevel_server(mcp).get_request_handler("resources/read")

            async def _go_v2():
                ctx = ServerRequestContext(
                    session=session,
                    lifespan_context=None,
                    protocol_version="2026-07-28",
                    method="resources/read",
                )
                return await entry.handler(
                    ctx,
                    ReadResourceRequestParams(
                        uri=local_stage.MESH_VIEWER_RESOURCE_URI
                    ),
                )

            return anyio.run(_go_v2)

        from types import SimpleNamespace

        from mcp.server.lowlevel.server import request_ctx

        async def _go():
            token = request_ctx.set(SimpleNamespace(session=session))
            try:
                return await mcp.read_resource(local_stage.MESH_VIEWER_RESOURCE_URI)
            finally:
                request_ctx.reset(token)

        return anyio.run(_go)

    def test_the_first_stage_read_announces_the_new_verb(self):
        _cache_the_stage()
        mcp = _fastmcp()
        local_stage.install(mcp)
        sent = []
        self._read_with_session(mcp, self._session(sent))
        assert "kiln_viewer_payload" in mcp._tool_manager._tools
        assert sent == [True], (
            "the verb was registered and the host was never told — a host "
            "that validates tool names cannot call what it has not listed"
        )

    def test_a_second_read_announces_nothing(self):
        """The list did not change, so saying it did would make a host
        re-list its tools once per panel."""
        _cache_the_stage()
        mcp = _fastmcp()
        local_stage.install(mcp)
        sent = []
        self._read_with_session(mcp, self._session(sent))
        self._read_with_session(mcp, self._session(sent))
        assert sent == [True]

    def test_a_failed_notice_never_sinks_the_document_read(self):
        """This runs inside the read that hands the host its stage.  A
        panel that fails to open beats a document that fails to arrive."""
        _cache_the_stage()
        mcp = _fastmcp()
        local_stage.install(mcp)
        doc = self._read_with_session(mcp, self._session([], fail=True))
        assert doc, "a refused notification took the stage document with it"
        assert "kiln_viewer_payload" in mcp._tool_manager._tools

    def test_the_read_registers_the_verb_on_every_sdk(self):
        """The half that works on both majors.

        Registration is what makes the fetch verb callable at all; the
        notification only tells a host that caches its tool list.  Pinned
        unconditionally so SDK 2 is not left with zero coverage of this
        class while the announce gap above is open.
        """
        _cache_the_stage()
        mcp = _fastmcp()
        local_stage.install(mcp)
        assert "kiln_viewer_payload" not in mcp._tool_manager._tools
        self._read_with_session(mcp, self._session([]))
        assert "kiln_viewer_payload" in mcp._tool_manager._tools

    def test_no_session_is_not_a_crash(self):
        """The diagnostics path and the tests read the document with no
        lowlevel context at all."""
        import anyio

        _cache_the_stage()
        mcp = _fastmcp()
        local_stage.install(mcp)
        assert anyio.run(mcp.read_resource, local_stage.MESH_VIEWER_RESOURCE_URI)


class TestPaintedThreeMfServesWithoutTrimeshLoader:
    """The panel's fetch verb serves a painted 3MF on the install
    ``pip install kiln3d`` produces — one without lxml or networkx, which
    trimesh's 3MF loader needs and a plain install never gets.

    Measured live 2026-09-01: ``paint_mesh_regions`` wrote a good painted
    3MF, the panel fetched it through ``kiln_viewer_payload``, the encoder
    died on ``No module named 'lxml'``, and the user saw "preview
    unavailable" over a file the PNG preview had already shown correctly.
    Every tool that returns a colored 3MF reaches the stage through this
    one verb, so this is the door test for all of them.
    """

    def test_the_verb_serves_a_painted_3mf(self, tmp_path, monkeypatch):
        import base64

        import anyio

        np = pytest.importorskip("numpy")
        trimesh = pytest.importorskip("trimesh")
        from trimesh.exceptions import ExceptionWrapper
        from trimesh.exchange import load as _load

        from kiln.multicolor_3mf import compose_painted_3mf

        mesh = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        mesh.apply_translation((100.0, 100.0, 5.0))
        tris = [tuple(map(tuple, mesh.vertices[f])) for f in mesh.faces]
        colors = ["#F72323" if all(v[2] > 9.9 for v in t) else "#2366F7" for t in tris]
        painted = tmp_path / "painted.3mf"
        compose_painted_3mf(tris, colors, output_path=str(painted))

        missing = ModuleNotFoundError("No module named 'lxml'", name="lxml")
        monkeypatch.setitem(_load.mesh_loaders, "3mf", ExceptionWrapper(missing))
        with pytest.raises(ModuleNotFoundError):
            trimesh.load(str(painted))  # the pin bites

        _cache_the_stage()
        mcp = _fastmcp()
        local_stage.install(mcp)
        anyio.run(mcp.read_resource, local_stage.MESH_VIEWER_RESOURCE_URI)
        fn = mcp._tool_manager._tools["kiln_viewer_payload"].fn
        served = fn(artifact_token=local_stage._mint(str(painted)))
        assert served.get("success") is not False, served
        payload = served[local_stage.VIEWER_STRUCTURED_CONTENT_KEY]
        assert payload["kind"] == "kiln.mesh.v1"
        assert payload["counts"]["triangles"] == 12
        rgba = np.frombuffer(
            base64.b64decode(payload["vertex_colors"]), dtype=np.uint8
        ).reshape(-1, 4)
        assert {tuple(c) for c in rgba.tolist()} == {
            (247, 35, 35, 255), (35, 102, 247, 255),
        }
