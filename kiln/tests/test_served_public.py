"""A signed-in install runs the manifest's served public tools on Kiln's servers.

On an install without kiln-pro, a public design or analysis tool named in the
bundled manifest's ``served_public`` block is run on Kiln's servers: its files
go up, the servers' answer comes back word for word, and every file it wrote
lands on this computer under a real path.  Signed out, offline or turned
away, the tool runs here and says why once per cause.

Driven through the real MCP dispatch with the bundled manifest; only the
network is stood in for (the served door ``_pro_api_call`` and httpx).

Coverage: the call sent (files, scad text, outputs set aside), the answer
kept word for word (a free band, a tool's own failure), files brought home
(a made model, a named output, prose that quotes a path), the four misses
and the once-per-cause note, and the calls that stay here (kiln-pro present,
a ``stays_here_when`` argument, a tool the block does not name, a missing
file).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import kiln.server as srv

UPLOADED = "upload0000000000000000000000001"
MADE = "made00000000000000000000000000001"
STL = b"solid part\nendsolid part\n"
SERVER_DIR = "/srv/kiln-scratch/kiln_output_x"


class _Response:
    def __init__(self, status=200, content=b"", body=None):
        self.status_code = status
        self.content = content
        self._body = body
        self.headers = {"content-type": "application/json" if body is not None else "application/octet-stream"}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _Servers:
    """Kiln's servers as this install sees them: uploads, tool calls, files."""

    def __init__(self):
        self.uploads: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.answer: dict = {}
        self.status = 200
        self.files: dict[str, bytes] = {}

    def post(self, url, **kwargs):
        upload = kwargs.get("files", {}).get("file")
        self.uploads.append({"url": url, "name": upload[0], "data": upload[1].read()})
        return _Response(body={"status": "success", "file_token": UPLOADED, "format": "stl"})

    def get(self, url, **_kwargs):
        token = url.rsplit("/", 1)[-1]
        if token in self.files:
            return _Response(content=self.files[token])
        return _Response(status=404, body={"error": "not found"})

    def door(self, tool_name, _timeout=None, _asked_by_user=True, _http=None, **kwargs):
        self.calls.append((tool_name, kwargs))
        if _http is not None:
            _http["status"] = self.status
        return json.loads(json.dumps(self.answer))


@pytest.fixture
def servers(monkeypatch, tmp_path):
    """A signed-in install with no kiln-pro, and Kiln's servers behind it.

    The bundled manifest is read the way a plain install reads it at start
    (the conftest default leaves no public tool served).
    """
    import httpx

    from kiln.mcp_compat import FastMCP

    srv._register_pro_tool_stubs(FastMCP("served-public-test"))
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.setenv("KILN_LICENSE_KEY", "signed-in-account")
    monkeypatch.setenv("KILN_API_URL", "https://kiln.test")
    module = sys.modules.get("kiln.served_public")
    if module is not None:
        monkeypatch.setattr(module, "_said", set())
    fake = _Servers()
    monkeypatch.setattr(srv, "_pro_api_call", fake.door)
    monkeypatch.setattr(httpx, "post", fake.post)
    monkeypatch.setattr(httpx, "get", fake.get)
    yield fake
    module = sys.modules.get("kiln.served_public")
    if module is not None:
        module._reset_for_tests()


@pytest.fixture
def part(tmp_path):
    path = tmp_path / "part.stl"
    path.write_bytes(STL)
    return path


def _call(name: str, **arguments):
    import asyncio

    return asyncio.run(
        srv.mcp._tool_manager.call_tool(name, arguments, context=None, convert_result=False)
    )


class TestTheCallGoesToKilnsServers:
    def test_the_file_goes_up_and_the_servers_answer_comes_back(self, servers, part):
        servers.answer = {"success": True, "report": {"score": 91, "grade": "A"}, "message": "from the servers"}
        result = _call("analyze_printability", file_path=str(part), material="petg")
        assert result == servers.answer
        assert [u["data"] for u in servers.uploads] == [STL]
        assert servers.uploads[0]["url"].endswith("/api/tool-inputs")
        (name, sent), = servers.calls
        assert name == "analyze_printability"
        assert sent == {"material": "petg", "file_tokens": {"file_path": {"token": UPLOADED, "name": "part.stl"}}}

    def test_an_openscad_file_is_sent_as_its_text(self, servers, tmp_path):
        scad = tmp_path / "box.scad"
        scad.write_text("cube([10, 10, 10]);\n")
        servers.answer = {"success": True, "message": "compiled"}
        _call("compile_scad", scad_path=str(scad))
        (_name, sent), = servers.calls
        assert sent == {"scad_code": "cube([10, 10, 10]);\n"}
        assert servers.uploads == []

    def test_a_tool_with_no_file_is_sent_as_it_was_called(self, servers):
        servers.answer = {"success": True, "recommendation": {"material": "asa"}}
        result = _call("recommend_material", intent="outdoor bracket")
        assert result == servers.answer
        assert servers.calls == [("recommend_material", {"intent": "outdoor bracket"})]

    def test_the_servers_free_band_is_the_answer_word_for_word(self, servers):
        servers.answer = {
            "status": "error",
            "code": "PRO_REQUIRED",
            "error": "Tax previews are part of Kiln Pro.",
            "upgrade_url": "https://kiln3d.com/pricing",
        }
        assert _call("tax_estimate", manufacturer_quote_usd=40.0, jurisdiction="US-CA") == servers.answer

    def test_a_tools_own_failure_is_its_answer_and_nothing_runs_here(self, servers, part):
        servers.answer = {"success": False, "error": "That file is not a model."}
        assert _call("analyze_printability", file_path=str(part)) == servers.answer
        assert len(servers.calls) == 1


class TestTheCallersPrinterGoesWithTheCall:
    """A tool that takes ``printer_id`` is answered for the caller's printer,
    and the servers have none: what the printer IS goes with the call."""

    @pytest.fixture
    def shop(self, monkeypatch):
        monkeypatch.setattr(srv, "_read_config_printers", lambda: {
            "shop-mini": {"type": "bambu", "printer_model": "bambu_a1_mini",
                          "host": "10.0.0.9", "access_code": "12345678"},
            "garage": {"type": "moonraker", "printer_model": "voron_2", "host": "voron.local"},
        })
        monkeypatch.setattr(srv, "_resolve_effective_printer_name", lambda *_a: "shop-mini")
        asked: list[str] = []

        def setting(name):
            asked.append(name)
            return 0.6, None

        monkeypatch.setattr("kiln.assumed_nozzle._printer_setting_mm", setting)
        return asked

    def test_the_printers_model_and_nozzle_go_never_its_address(self, servers, part, shop):
        servers.answer = {"success": True, "report": {}}
        _call("analyze_printability", file_path=str(part), printer_id="garage")
        (name, sent), = servers.calls
        facts = sent["step_results"]["printer_facts"]["data"]
        assert {p["name"]: (p["model"], p.get("nozzle_setting_mm")) for p in facts["printers"]} == {
            "shop-mini": ("bambu_a1_mini", None), "garage": ("voron_2", 0.6),
        }
        assert shop == ["garage"]
        assert not {"10.0.0.9", "12345678", "voron.local"} & set(json.dumps(sent).split('"'))

    def test_a_call_that_states_its_nozzle_does_not_read_the_printers(self, servers, part, shop):
        servers.answer = {"success": True, "report": {}}
        _call("analyze_printability", file_path=str(part), printer_id="garage", nozzle_diameter=0.8)
        printers = servers.calls[0][1]["step_results"]["printer_facts"]["data"]["printers"]
        assert shop == [] and not any("nozzle_setting_mm" in p for p in printers)
        assert {p["name"]: p["model"] for p in printers}["garage"] == "voron_2"

    def test_an_unnamed_call_reads_the_default_printer(self, servers, part, shop):
        servers.answer = {"success": True, "report": {}}
        _call("analyze_printability", file_path=str(part))
        assert shop == ["shop-mini"] and "step_results" in servers.calls[0][1]

    def test_a_tool_that_names_no_printer_sends_none(self, servers, shop):
        servers.answer = {"success": True}
        _call("tax_estimate", manufacturer_quote_usd=40.0, jurisdiction="US-CA")
        assert "step_results" not in servers.calls[0][1] and shop == []

    def test_a_reading_this_computer_refuses_sends_nothing_and_runs_here(self, servers, part, monkeypatch):
        from kiln import served_steps

        monkeypatch.setattr(served_steps, "read_first", lambda *_a, **_k: ({}, {
            "status": "error", "code": "LOCAL_STEP_REFUSED", "error": "not handed over",
        }))
        result = _call("analyze_printability", file_path=str(part), printer_id="garage")
        assert "analyze_printability" not in [name for name, _ in servers.calls]
        assert result["answered_on_this_computer"]["why"] == "refused"


class TestTheFilesItWroteComeHome:
    def test_a_made_model_lands_here_where_the_answer_names_it(self, servers, tmp_path):
        theirs = f"{SERVER_DIR}/openscad-1.stl"
        servers.answer = {
            "success": True,
            "stl_path": theirs,
            "message": f"Compiled to {theirs}",
            "artifact": {"artifact_token": MADE, "format": "stl", "at": ["stl_path"]},
        }
        servers.files[MADE] = STL
        result = _call("compile_scad", scad_code="cube(10);")
        ours = Path(result["stl_path"])
        assert ours.read_bytes() == STL
        assert ours.is_relative_to(tmp_path / "kiln-home" / "served" / "files")
        assert result["message"] == f"Compiled to {ours}"
        assert "artifact" not in result
        assert SERVER_DIR not in json.dumps(result)
        assert result["files"] == [
            {"filename": ours.name, "format": "stl", "path": str(ours), "on_this_computer": True}
        ]

    def test_a_made_model_arrives_with_the_notes_kept_with_it(self, servers, monkeypatch):
        """The layer a part was designed in is kept with it on the servers
        and written beside the copy here, so a slice here prints in it."""
        import hashlib

        import httpx

        from kiln.slicer_layer import read as read_layer

        theirs = f"{SERVER_DIR}/openscad-1.stl"
        servers.answer = {
            "success": True,
            "stl_path": theirs,
            "artifact": {"artifact_token": MADE, "format": "stl", "at": ["stl_path"]},
        }
        servers.files[MADE] = STL
        note = {"schema": 1, "file": hashlib.sha256(STL).hexdigest()[:32],
                "layer": {"height_mm": 0.2, "levels": 8, "depth_mm": 1.6}}

        def get(url, **kwargs):
            if url.endswith(f"/{MADE}/notes"):
                return _Response(body={"notes": {".layer.json": note}})
            return servers.get(url, **kwargs)

        monkeypatch.setattr(httpx, "get", get)
        result = _call("compile_scad", scad_code="cube(10);")
        assert read_layer(result["stl_path"]).height_mm == 0.2

    def test_an_output_the_call_named_is_saved_exactly_there(self, servers, part, tmp_path):
        wanted = tmp_path / "out" / "fixed.stl"
        theirs = f"{SERVER_DIR}/fixed.stl"
        servers.answer = {
            "success": True,
            "output_path": theirs,
            "files": [{
                "at": ["output_path"], "param": "output_path", "filename": "fixed.stl",
                "format": "stl", "artifact_token": MADE,
            }],
        }
        servers.files[MADE] = STL
        result = _call("check_print_readiness", file_path=str(part), auto_fix=True, output_path=str(wanted))
        (_name, sent), = servers.calls
        assert "output_path" not in sent and sent["output_names"] == {"output_path": "fixed.stl"}
        assert result["output_path"] == str(wanted)
        assert wanted.read_bytes() == STL

    def test_a_made_file_that_does_not_arrive_is_run_here_instead(self, servers, monkeypatch):
        servers.answer = {
            "success": True,
            "stl_path": f"{SERVER_DIR}/openscad-1.stl",
            "artifact": {"artifact_token": MADE, "format": "stl", "at": ["stl_path"]},
        }
        here = {"success": True, "stl_path": "/here/openscad.stl", "message": "made here"}
        _run_here_answers(monkeypatch, "compile_scad", here)
        result = _call("compile_scad", scad_code="cube(10);")
        assert result["message"] == "made here"
        assert result["answered_on_this_computer"]["why"] == "unanswered"


class TestOtherwiseItRunsHereAndSaysWhyOnce:
    def test_signed_out_runs_here_and_says_so_once(self, servers, monkeypatch, part):
        monkeypatch.delenv("KILN_LICENSE_KEY")
        monkeypatch.setattr(srv, "_served_bearer", lambda: ("", None), raising=False)
        _run_here_answers(monkeypatch, "recommend_material", {"success": True, "from": "here"})
        _run_here_answers(monkeypatch, "recommend_design_material", {"success": True, "from": "here"})
        first = _call("recommend_material", intent="outdoor bracket")
        second = _call("recommend_design_material", requirements="food bin")
        assert servers.calls == [] and servers.uploads == []
        note = first["answered_on_this_computer"]
        assert note["why"] == "signed_out"
        assert note["note"] == (
            "This answer comes from this computer alone, without what Kiln's servers add to it. "
            "Kiln can't run recommend_material on its servers right now because Kiln is signed out, "
            "so it answered here instead. Sign in and ask again."
        )
        assert "answered_on_this_computer" not in second

    def test_offline_runs_here_and_says_so(self, servers, monkeypatch):
        from kiln.served_answer import envelope_for_transport

        def offline(tool_name, _timeout=None, _asked_by_user=True, _http=None, **kwargs):
            import socket

            servers.calls.append((tool_name, kwargs))
            return envelope_for_transport(tool_name, socket.gaierror("no route"), host="https://kiln.test")

        monkeypatch.setattr(srv, "_pro_api_call", offline)
        _run_here_answers(monkeypatch, "recommend_material", {"success": True, "from": "here"})
        result = _call("recommend_material", intent="outdoor bracket")
        assert result["from"] == "here"
        assert result["answered_on_this_computer"]["why"] == "offline"
        assert "Reconnect to the internet and ask again." in result["answered_on_this_computer"]["note"]

    def test_a_file_that_cannot_reach_the_servers_says_offline(self, servers, monkeypatch, part):
        import socket

        import httpx

        def no_route(url, **_kwargs):
            try:
                raise socket.gaierror(8, "nodename nor servname provided")
            except socket.gaierror as exc:
                raise httpx.ConnectError("no route") from exc

        monkeypatch.setattr(httpx, "post", no_route)
        _run_here_answers(monkeypatch, "analyze_printability", {"success": True, "from": "here"})
        result = _call("analyze_printability", file_path=str(part))
        assert result["from"] == "here" and servers.calls == []
        assert result["answered_on_this_computer"]["why"] == "offline"

    def test_turned_away_runs_here_and_passes_on_the_servers_words(self, servers, monkeypatch):
        """The servers' version floor, as it answers: the person's sentence is
        in the upgrade block, the error field is a code word."""
        servers.status = 426
        servers.answer = {
            "success": False,
            "error": "upgrade_required",
            "code": "UPGRADE_REQUIRED",
            "upgrade": {"headline": "Kiln 9.9.9 or newer is required to continue.", "target": "9.9.9"},
        }
        _run_here_answers(monkeypatch, "recommend_material", {"success": True, "from": "here"})
        result = _call("recommend_material", intent="outdoor bracket")
        assert result["from"] == "here"
        note = result["answered_on_this_computer"]
        assert note["why"] == "refused"
        assert note["note"].endswith(
            "because Kiln's servers said no, so it answered here instead. "
            "Kiln 9.9.9 or newer is required to continue."
        )


class TestCallsThatStayHere:
    def test_kiln_pro_in_the_process_keeps_every_call_here(self, servers, monkeypatch):
        from kiln import served_public

        monkeypatch.setattr(served_public, "_kiln_pro_here", lambda: True)
        _run_here_answers(monkeypatch, "recommend_material", {"success": True, "from": "here"})
        result = _call("recommend_material", intent="outdoor bracket")
        assert servers.calls == [] and result == {"success": True, "from": "here"}

    def test_an_argument_that_reads_this_computer_keeps_the_call_here(self, servers, monkeypatch):
        _run_here_answers(monkeypatch, "recommend_material", {"success": True, "from": "here"})
        result = _call("recommend_material", intent="outdoor bracket", on_hand_only=True, printer_id="a1")
        assert servers.calls == [] and result == {"success": True, "from": "here"}

    def test_a_tool_the_block_does_not_name_is_never_sent(self, servers, monkeypatch):
        _run_here_answers(monkeypatch, "list_available_materials", {"success": True, "from": "here"})
        assert _call("list_available_materials") == {"success": True, "from": "here"}
        assert servers.calls == []

    def test_a_file_not_on_this_computer_is_left_to_the_tool_to_say(self, servers, monkeypatch, tmp_path):
        _run_here_answers(monkeypatch, "compile_scad", {"success": False, "error": "File not found"})
        result = _call("compile_scad", scad_path=str(tmp_path / "missing.scad"))
        assert result == {"success": False, "error": "File not found"}
        assert servers.calls == []


class TestAJobIsAskedAboutSoon:
    def test_the_first_ask_comes_quickly_then_less_often(self, monkeypatch):
        """A heavy tool runs as a job; most finish in about a second, so the
        first ask is not three seconds away."""
        import io
        import time
        import urllib.request

        answers = [{"status": "running"}] * 3 + [{"success": True, "made": "it"}]
        slept: list[float] = []

        class _Resp(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        monkeypatch.setenv("KILN_LICENSE_KEY", "signed-in-account")
        monkeypatch.setattr(time, "sleep", slept.append)
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda req, timeout=None: _Resp(json.dumps(answers.pop(0)).encode()),
        )
        answer = srv._await_served_job(
            "add_mesh_fillet", "https://kiln.test", {}, {"job_id": "j1", "poll": "/api/tools/jobs/j1"},
        )
        assert answer == {"success": True, "made": "it"}
        assert slept == [0.5, 1.0, 2.0, 3.0]


class TestTheBlockNamesPublicTools:
    def test_every_named_tool_is_a_public_tool_here(self):
        manifest = json.loads((Path(srv.__file__).parent / "pro_tool_manifest.json").read_text())
        block = manifest["served_public"]
        assert block["schema_version"] == 1 and block["tools"]
        for name in block["tools"]:
            tool = srv.mcp._tool_manager.get_tool(name)
            assert tool is not None, name
            assert not tool.fn.__module__.startswith("kiln_pro"), name


def _run_here_answers(monkeypatch, name: str, answer: dict) -> None:
    """The tool, run on this computer, answers *answer*."""
    tool = srv.mcp._tool_manager.get_tool(name)
    real_run = type(tool).run

    async def run(self, arguments, context=None, convert_result=False):
        if self.name != name:
            return await real_run(self, arguments, context, convert_result=convert_result)
        result = json.loads(json.dumps(answer))
        return self.fn_metadata.convert_result(result) if convert_result else result

    monkeypatch.setattr(type(tool), "run", run)
