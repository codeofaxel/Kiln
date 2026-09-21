"""The inline 3D stage chain never fails silently.

Three measured failures, one story — 2026-09-19.  The host declared MCP
Apps at initialize, the panel HTML loaded, and not one
``kiln_viewer_payload`` call reached the server all day: the host had
cached its tool list before a ``restart_server`` and never re-listed.  So
the panel could not fetch, the make results carried only a token the
panel could not redeem, and nobody — not the user, not the agent, not the
log — was told.  Each layer below now says what it knows:

* the stage layer notices a mint whose fetch never came and puts the
  browser stage link on the NEXT result, as if the host drew no panel;
* ``restart_server`` says in its result that every already-open session
  loses the stage until that client reconnects, and how;
* the link door logs every refusal, and ``last_refusal`` returns the
  recorded reason so a caller can say why there is no link.

Clocks are monkeypatched, never slept on.
"""

from __future__ import annotations

import json
import logging
import struct
import time

import anyio
import pytest

from kiln import local_stage, preview_evidence, stage_cache, stage_link

_DOC = "<!DOCTYPE html><html><body>stage</body></html>"
_UI = local_stage.MCP_APPS_EXTENSION_ID


# ---------------------------------------------------------------------------
# Fakes — the same shapes test_local_stage.py and test_stage_link.py use
# ---------------------------------------------------------------------------


def _stl(path, triangles: int = 2):
    data = bytearray(b"\x00" * 80) + struct.pack("<I", triangles)
    data += b"\x00" * (50 * triangles)
    path.write_bytes(bytes(data))
    return str(path)


def _real_cube(path):
    trimesh = pytest.importorskip("trimesh")
    trimesh.creation.box(extents=(20.0, 20.0, 20.0)).export(str(path))
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


def _apps_host():
    return _Host(_Caps(extensions={_UI: {}}))


def _silent_host():
    return _Host(_Caps())


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {
            "viewer_url": "https://app.kiln3d.com/view#v=tok",
            "expires_in": 1800,
        }

    def json(self):
        return self._body


def _wire_link(monkeypatch, resp=None, token="bearer-abc"):
    """Point the link door at a fake API and a signed-in bearer; return the
    list every upload lands in."""
    monkeypatch.setattr(
        "kiln.auth_session.resolve_api_bearer",
        lambda *a, **k: type("B", (), {"token": token, "state": "license"})(),
    )
    calls: list[dict] = []

    def _post(url, **kw):
        calls.append({"url": url, "headers": kw.get("headers", {})})
        return resp or _Resp()

    import httpx

    monkeypatch.setattr(httpx, "post", _post)
    return calls


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln_home"))
    monkeypatch.delenv(local_stage._OPT_OUT_ENV, raising=False)
    monkeypatch.delenv(local_stage._DIAGNOSTICS_ENV, raising=False)
    monkeypatch.delenv("KILN_STAGE_INLINE_GEOMETRY", raising=False)
    monkeypatch.delenv(stage_link._OPT_OUT_ENV, raising=False)
    # The link door is signed OUT by default: the hook now tries the link
    # whenever the panel is unproven, and a real bearer here would upload a
    # test mesh to the live service.  Tests that want a link wire one.
    monkeypatch.setattr(
        "kiln.auth_session.resolve_api_bearer",
        lambda *a, **k: type("B", (), {"token": "", "state": "signed_out"})(),
    )
    from kiln import stage_link as _stage_link

    _stage_link._cache.clear()
    _stage_link._REFUSED_BEARER = None
    stage_link._cache.clear()
    stage_link._REFUSED_BEARER = None
    local_stage._reset_for_tests()
    stage_cache._reset_for_tests()
    preview_evidence._reset_for_tests()
    yield
    stage_link._cache.clear()
    stage_link._REFUSED_BEARER = None
    local_stage._reset_for_tests()
    stage_cache._reset_for_tests()
    preview_evidence._reset_for_tests()


@pytest.fixture
def clock(monkeypatch):
    """The stage's monotonic clock, advanced by hand."""
    now = [1000.0]
    monkeypatch.setattr(local_stage, "_now", lambda: now[0])

    class _Clock:
        def advance(self, seconds: float) -> None:
            now[0] += seconds

        @property
        def value(self) -> float:
            return now[0]

    return _Clock()


def _fastmcp():
    from kiln.mcp_compat import FastMCP

    mcp = FastMCP("test")

    @mcp.tool(name="compile_scad")
    def compile_scad() -> dict:
        return {"success": True}

    @mcp.tool(name="list_materials")
    def list_materials() -> dict:
        return {"success": True}

    return mcp


def _installed():
    (stage_cache.cache_dir() / "mesh_viewer.html").write_text(_DOC, encoding="utf-8")
    stage_cache._reset_for_tests()
    mcp = _fastmcp()
    local_stage.install(mcp)
    return mcp


def _make(host, mesh_path, tool_name="compile_scad"):
    """Drive the real lowlevel hook over one make's result and return its
    structuredContent — a fresh server per make, module state shared, the
    way a host's repeated calls into one process are."""
    from kiln.mcp_compat import MCP_SDK_MAJOR, lowlevel_server

    (stage_cache.cache_dir() / "mesh_viewer.html").write_text(_DOC, encoding="utf-8")
    stage_cache._reset_for_tests()
    mcp = _fastmcp()
    result = _Result({"success": True, "message": "made a thing", "stl_path": mesh_path})
    server = lowlevel_server(mcp)

    if MCP_SDK_MAJOR >= 2:
        from mcp.types import CallToolRequestParams

        params = CallToolRequestParams(name=tool_name, arguments={})
        entry = server.get_request_handler("tools/call")

        async def _base(_ctx, _params):
            return result

        server.add_request_handler("tools/call", entry.params_type, _base)
        local_stage.install(mcp)
        handler = server.get_request_handler("tools/call").handler
        anyio.run(handler, host._mcp_server.request_context, params)
        return result.structuredContent

    from mcp.server.lowlevel.server import request_ctx
    from mcp.types import CallToolRequest, CallToolRequestParams

    handlers = server.request_handlers

    async def _base_v1(_req):
        return type("R", (), {"root": result})()

    handlers[CallToolRequest] = _base_v1
    local_stage.install(mcp)
    req = CallToolRequest(
        method="tools/call", params=CallToolRequestParams(name=tool_name, arguments={})
    )
    token = request_ctx.set(host._mcp_server.request_context)
    try:
        anyio.run(handlers[CallToolRequest], req)
    finally:
        request_ctx.reset(token)
    return result.structuredContent


def _fetch(token):
    """What the rendered panel does: call the fetch verb with its token."""
    mcp = _installed()
    return mcp._tool_manager._tools["kiln_viewer_payload"].fn(artifact_token=token)


# ---------------------------------------------------------------------------
# 1. A panel that opens but cannot fetch triggers the link fallback
# ---------------------------------------------------------------------------


class TestAPanelThatCannotFetchFallsBackToTheLink:
    def test_a_mint_nobody_fetched_keeps_the_link_riding(
        self, tmp_path, monkeypatch, clock, caplog
    ):
        """THE INCIDENT.  The host declared apps, the panel opened, the
        fetch never came.  The FIRST result already carries the link — no
        panel has proved itself yet (2026-09-21: the first make after a
        restart rode alone) — and once the grace judges the mint unfetched
        the next result says WHY the link rides, and the log says so."""
        calls = _wire_link(monkeypatch)
        caplog.set_level(logging.INFO, logger="kiln.local_stage")

        first = _make(_apps_host(), _real_cube(tmp_path / "a.stl"))
        assert first["artifact"]["artifact_token"]
        assert first.get("viewer_url"), "no panel has proved itself: the first make rode alone"
        assert first["shown"]["door"] == "link" and "stage_fallback" not in first
        assert len(calls) == 1

        clock.advance(local_stage._FETCH_GRACE_S + 1.0)

        second = _make(_apps_host(), _real_cube(tmp_path / "b.stl"))
        assert second.get("viewer_url") == "https://app.kiln3d.com/view#v=tok", (
            "the panel could not fetch and the result still had no stage"
        )
        assert second["success"] is True and second["stl_path"].endswith("b.stl"), (
            "the tool's own output must survive the fallback"
        )
        assert "stage_fallback" in second, "the result must say WHY the link rides"
        assert second["shown"]["door"] == "link"
        # b.stl is byte-identical to a.stl: the content-addressed cache
        # answers the second link, so the service saw one upload.
        assert len(calls) == 1
        assert local_stage.panel_fetches_stalled() is True
        assert any(
            "fetch" in r.getMessage() and "reconnect" in r.getMessage()
            for r in caplog.records
        ), "the flip is not in the log"

    def test_a_fetch_inside_the_grace_keeps_results_lean(
        self, tmp_path, monkeypatch, clock
    ):
        """The measured hosts fetch immediately on render; a working panel
        must cost nothing extra."""
        calls = _wire_link(monkeypatch)
        first = _make(_apps_host(), _real_cube(tmp_path / "a.stl"))
        assert len(calls) == 1, "the first make rides with the link until a panel proves itself"
        served = _fetch(first["artifact"]["artifact_token"])
        assert served.get("success") is not False, served

        clock.advance(local_stage._FETCH_GRACE_S + 1.0)

        second = _make(_apps_host(), _real_cube(tmp_path / "b.stl"))
        assert "viewer_url" not in second
        assert len(calls) == 1, "a proven panel paid for a second upload"
        assert local_stage.panel_fetches_stalled() is False

    def test_a_successful_fetch_clears_the_flag(self, tmp_path, monkeypatch, clock):
        """The client reconnected (or the host finally re-listed): the first
        fetch that lands turns the fallback off again."""
        calls = _wire_link(monkeypatch)
        _make(_apps_host(), _real_cube(tmp_path / "a.stl"))
        clock.advance(local_stage._FETCH_GRACE_S + 1.0)
        second = _make(_apps_host(), _real_cube(tmp_path / "b.stl"))
        assert "viewer_url" in second and local_stage.panel_fetches_stalled() is True

        served = _fetch(second["artifact"]["artifact_token"])
        assert served.get("success") is not False, served
        assert local_stage.panel_fetches_stalled() is False
        assert local_stage.panel_proven() is True

        clock.advance(local_stage._FETCH_GRACE_S + 1.0)
        third = _make(_apps_host(), _real_cube(tmp_path / "c.stl"))
        assert "viewer_url" not in third, "the flag stuck after a fetch arrived"
        assert len(calls) == 1, "identical cubes share one upload; the third rode lean"

    def test_a_host_that_never_declared_apps_gets_the_link_as_its_stage(
        self, tmp_path, monkeypatch, clock
    ):
        """No panel was ever expected, so no fetch is missing and nothing
        stalls — and a host with no panel never proves one, so for it the
        browser link is the stage, on every result."""
        calls = _wire_link(monkeypatch)
        first = _make(_silent_host(), _real_cube(tmp_path / "a.stl"))
        assert first.get("viewer_url") and first["shown"]["door"] == "link"
        clock.advance(local_stage._FETCH_GRACE_S + 1.0)
        second = _make(_silent_host(), _real_cube(tmp_path / "b.stl"))
        assert second["artifact"]["artifact_token"]
        assert second.get("viewer_url")
        assert "stage_fallback" not in second, "nothing stalled — no panel was promised"
        assert len(calls) == 1, "identical cubes share one upload"
        assert local_stage.panel_fetches_stalled() is False

    def test_a_tool_that_opens_no_panel_is_not_a_missing_fetch(
        self, tmp_path, monkeypatch, clock
    ):
        """An unstamped tool (list_materials is on no roster) opens no panel,
        so its token going unfetched proves nothing about the panel."""
        calls = _wire_link(monkeypatch)
        first = _make(_apps_host(), _real_cube(tmp_path / "a.stl"), tool_name="list_materials")
        assert first is None or "artifact" not in first, "an unstamped tool minted a token"
        assert calls == []
        clock.advance(local_stage._FETCH_GRACE_S + 1.0)
        second = _make(_apps_host(), _real_cube(tmp_path / "b.stl"))
        assert "stage_fallback" not in second, "an unfetched mint nobody expected read as a stall"
        assert local_stage.panel_fetches_stalled() is False

    def test_a_fetch_answered_by_a_sibling_server_is_not_a_stall(
        self, tmp_path, monkeypatch, clock
    ):
        """A desktop host routes the panel's fetch over whichever session's
        connection it holds (measured 2026-09-01).  The minting process
        never sees that fetch — but the sibling wrote the machine-wide
        stage record, and that is what must be believed."""
        calls = _wire_link(monkeypatch)
        mesh = _real_cube(tmp_path / "a.stl")
        _make(_apps_host(), mesh)
        assert len(calls) == 1, "unproven: the first make rides with the link"
        # The sibling server served the panel: its record, not ours.
        preview_evidence.record("stage", mesh, via="panel_fetch")

        clock.advance(local_stage._FETCH_GRACE_S + 1.0)
        second = _make(_apps_host(), _real_cube(tmp_path / "b.stl"))
        assert "viewer_url" not in second, "a working panel was called broken"
        assert len(calls) == 1
        assert local_stage.panel_fetches_stalled() is False
        assert local_stage.panel_proven() is True, "a sibling's fetch proves the panel"

    def test_the_hook_never_waits_for_the_fetch(self, tmp_path, monkeypatch, clock):
        """The grace is measured between calls, never slept through inside
        one: a tool call that waited for the panel would stall every host
        that has no panel at all."""
        _wire_link(monkeypatch)

        def _no_sleep(*_a, **_k):
            raise AssertionError("the hook slept waiting for a fetch")

        monkeypatch.setattr(time, "sleep", _no_sleep)
        before = clock.value
        _make(_apps_host(), _real_cube(tmp_path / "a.stl"))
        _make(_apps_host(), _real_cube(tmp_path / "b.stl"))
        assert clock.value == before

    def test_the_fetch_verbs_lean_contract_is_untouched(self, tmp_path, clock):
        """The verb serves exactly the payload block it always did — the
        bookkeeping rides beside it, never inside it."""
        first = _make(_apps_host(), _real_cube(tmp_path / "a.stl"))
        served = _fetch(first["artifact"]["artifact_token"])
        assert set(served) == {local_stage.VIEWER_STRUCTURED_CONTENT_KEY}
        assert served[local_stage.VIEWER_STRUCTURED_CONTENT_KEY]["kind"] == "kiln.mesh.v1"
        assert _fetch("no-such-token") == {
            "success": False, "error": "Unknown or expired viewer token."
        }

    def test_visualize_model_reads_the_same_flag(self, tmp_path, monkeypatch, clock):
        """The render door already attaches the link; while fetches are not
        arriving it must say so in the same words, so an agent reading
        either result learns the stage is the link today."""
        _wire_link(monkeypatch)
        from kiln.model_visualizer import visualize_model

        mesh = _real_cube(tmp_path / "a.stl")
        _make(_apps_host(), mesh)
        clock.advance(local_stage._FETCH_GRACE_S + 1.0)
        assert local_stage.panel_fetches_stalled() is True

        # The renderer is not the question: a fake openscad writes the PNG
        # it was asked for, the same stub the sign-off tests use.
        import pathlib
        import subprocess
        from unittest.mock import MagicMock

        def _run(cmd, **kwargs):
            for i, arg in enumerate(cmd):
                if arg == "-o" and i + 1 < len(cmd):
                    pathlib.Path(cmd[i + 1]).write_bytes(b"png")
            m = MagicMock()
            m.returncode = 0
            return m

        monkeypatch.setattr("kiln.model_visualizer._find_openscad", lambda: "openscad")
        monkeypatch.setattr(subprocess, "run", _run)
        result = visualize_model(
            mesh, output_dir=str(tmp_path / "out"), allow_stage=False, angles=["isometric"]
        )
        assert result["success"] is True, result
        assert result.get("viewer_url"), "the render door stopped attaching the link"
        assert result.get("stage_fallback"), result
        assert result["stage_fallback"] == local_stage.panel_fetch_fallback_note()


# ---------------------------------------------------------------------------
# 2. restart_server says what every open session loses
# ---------------------------------------------------------------------------


class TestRestartServerSaysWhatOpenSessionsLose:
    def test_the_result_names_the_loss_and_the_remedy(self, monkeypatch, caplog):
        """Measured on Claude Desktop 2026-09-01: the host does not re-list
        tools on ``notifications/tools/list_changed``, so a session that
        was open before the restart keeps a tool list without
        ``kiln_viewer_payload`` — no panel can fetch — until that client
        reconnects.  The tool that causes it has to say so."""
        import threading

        from kiln import server

        class _NoThread:
            def __init__(self, *a, **k):
                pass

            def start(self):  # the real one execs over pytest
                return None

        monkeypatch.setattr(threading, "Thread", _NoThread)
        caplog.set_level(logging.INFO, logger="kiln.server")

        out = server.restart_server(clean_env=False)
        assert out["success"] is True
        text = " ".join(str(v) for v in out.values()).lower()
        assert "stage" in text and "reconnect" in text, out
        assert "new chat" in text or "new conversation" in text, out
        assert any(
            "stage" in r.getMessage().lower() and "reconnect" in r.getMessage().lower()
            for r in caplog.records
        ), "the loss is not in the server log"


# ---------------------------------------------------------------------------
# 3. The link door says why it refused
# ---------------------------------------------------------------------------


class TestLinkRefusalsAreSaidOutLoud:
    @pytest.fixture(autouse=True)
    def _debug_log(self, caplog):
        caplog.set_level(logging.DEBUG, logger="kiln.stage_link")

    def _refusal_lines(self, caplog):
        return [
            r.getMessage() for r in caplog.records
            if r.name == "kiln.stage_link" and "refus" in r.getMessage().lower()
        ]

    def test_opted_out_is_recorded_logged_and_readable(
        self, tmp_path, monkeypatch, caplog
    ):
        _wire_link(monkeypatch)
        monkeypatch.setenv(stage_link._OPT_OUT_ENV, "1")
        mesh = _stl(tmp_path / "part.stl")
        assert stage_link.stage_link_for(mesh) is None, "the None contract moved"
        assert stage_link.last_refusal(mesh) == "opted_out"
        lines = self._refusal_lines(caplog)
        assert len(lines) == 1, lines
        assert "opted_out" in lines[0] and "part.stl" in lines[0]

    def test_signed_out_is_recorded_logged_and_readable(
        self, tmp_path, monkeypatch, caplog
    ):
        _wire_link(monkeypatch, token="")
        mesh = _stl(tmp_path / "part.stl")
        assert stage_link.stage_link_for(mesh) is None
        assert stage_link.last_refusal(mesh) == "signed_out"
        assert any("signed_out" in line for line in self._refusal_lines(caplog))

    def test_an_api_error_is_recorded_with_its_status(
        self, tmp_path, monkeypatch, caplog
    ):
        _wire_link(monkeypatch, resp=_Resp(status=503))
        mesh = _stl(tmp_path / "part.stl")
        assert stage_link.stage_link_for(mesh) is None
        assert stage_link.last_refusal(mesh) == "http_503"
        assert any("http_503" in line for line in self._refusal_lines(caplog))

    def test_a_transport_failure_is_recorded_as_which_of_two_things(self, tmp_path, monkeypatch):
        """No route is offline; a server that did not answer is unanswered.
        The two have different fixes, so the record keeps them apart -- and
        it never opens a second socket to tell them apart."""
        import socket

        import httpx

        _wire_link(monkeypatch)

        def _no_answer(*a, **k):
            raise OSError("network down")

        monkeypatch.setattr(httpx, "post", _no_answer)
        mesh = _stl(tmp_path / "part.stl")
        assert stage_link.stage_link_for(mesh) is None
        assert stage_link.last_refusal(mesh) == "unanswered"
        assert "didn't answer" in stage_link.refusal_sentence(stage_link.last_refusal(mesh))

        def _no_route(*a, **k):
            raise httpx.ConnectError("dns") from socket.gaierror(8, "nodename nor servname provided")

        monkeypatch.setattr(httpx, "post", _no_route)
        offline = _stl(tmp_path / "other.stl")
        assert stage_link.stage_link_for(offline) is None
        assert stage_link.last_refusal(offline) == "offline"
        assert "this computer is offline" in stage_link.refusal_sentence("offline")

    def test_a_mesh_never_tried_has_no_refusal(self, tmp_path):
        assert stage_link.last_refusal(_stl(tmp_path / "fresh.stl")) is None

    def test_a_link_issued_after_the_refusal_clears_it(self, tmp_path, monkeypatch):
        """Signed out, then signed in: the newer fact wins, or a caller
        would explain a refusal that no longer holds."""
        _wire_link(monkeypatch, token="")
        mesh = _stl(tmp_path / "part.stl")
        assert stage_link.stage_link_for(mesh) is None
        assert stage_link.last_refusal(mesh) == "signed_out"
        _wire_link(monkeypatch)
        assert stage_link.stage_link_for(mesh)["viewer_url"]
        assert stage_link.last_refusal(mesh) is None

    def test_last_refusal_never_raises(self, monkeypatch):
        monkeypatch.setattr(
            "kiln.preview_evidence.evidence_for",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ledger on fire")),
        )
        assert stage_link.last_refusal("/nope/none.stl") is None
