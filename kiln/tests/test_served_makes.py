"""A make from Kiln's servers reaches a person on a plain install.

A served tool is forwarded by a stub, and a forwarded call carries text.
Before ``kiln.served_makes`` a coaster generated this way answered
"success" with ``output_stl`` set to a path on the SERVER, no picture, no
way to keep it, and no way to hand a served tool a model from this
computer.  The agent's next step, slicing that path, failed with "file not
found".  Nobody had met it because the stubs had never registered on a
published install.

These walk the loop through the registered tools (the stub, the stage's
token read, ``keep_design``), with only the network stood in for:

* a make arrives with no path that is not on this computer, says where it
  is, and its look is fetched for the stage without riding the answer;
* the file lands on disk only when kept, the keep is the server's to
  charge, and a refused keep (the allowance is spent) saves nothing;
* a model or image on this computer is sent up before the call, and a make
  still on the servers is named by its token with nothing uploaded;
* a tool the servers refuse to run is not listed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiln import served_makes

TOKEN = "QzN57tLRbyH2N-ZAHsJrK3vJX_k51thF"
LOOK_BYTES = b"solid look\nendsolid look\n"
FULL_BYTES = b"solid full\n" + b"facet\n" * 40 + b"endsolid full\n"
SERVER_STL = "/tmp/kiln_generate_coaster_klrlycuy/coaster.stl"


def _served_answer(**extra) -> dict:
    return {
        "status": "success",
        "message": "Generated 90mm coaster (7.0mm thick, PLA).",
        "output_stl": SERVER_STL,
        "scad_path": "/tmp/kiln_generate_coaster_klrlycuy/coaster.scad",
        "artifact": {
            "artifact_token": TOKEN,
            "stl_url": f"/api/artifact/{TOKEN}",
            "format": "stl",
            "expires_in": 1800,
        },
        **extra,
    }


class _Response:
    def __init__(self, status=200, content=b"", body=None, headers=None):
        self.status_code = status
        self.content = content
        self._body = body
        self.headers = {"content-type": "model/stl", **(headers or {})}
        if body is not None:
            self.headers["content-type"] = "application/json"

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture(autouse=True)
def this_computer(monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.setattr(served_makes, "_bearer", lambda: "bearer-token")
    return tmp_path


@pytest.fixture
def wire(monkeypatch):
    """Stand in for Kiln's servers: record each request, answer from a table."""
    import httpx

    calls: list[tuple[str, str, dict]] = []
    answers: dict[tuple[str, str], _Response] = {
        ("GET", f"/api/artifact/{TOKEN}"): _Response(content=LOOK_BYTES),
        ("POST", f"/api/artifact/{TOKEN}/keep"): _Response(
            content=FULL_BYTES,
            headers={
                "content-disposition": 'attachment; filename="model.stl"',
                "X-Kiln-Keep-Used": "1",
                "X-Kiln-Keep-Limit": "3",
                "X-Kiln-Keep-Remaining": "2",
                "X-Kiln-Keep-Bucket": "generator",
            },
        ),
    }

    def _answer(method):
        def call(url, **kwargs):
            path = url.split("api.kiln3d.com", 1)[-1]
            calls.append((method, path, kwargs))
            return answers.get((method, path), _Response(status=404, body={"error": "not_found"}))

        return call

    monkeypatch.setattr(httpx, "get", _answer("GET"))
    monkeypatch.setattr(httpx, "post", _answer("POST"))
    return calls, answers


def _paths_in(value) -> list[str]:
    """Every absolute file path anywhere in an answer."""
    found: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            found += _paths_in(item)
    elif isinstance(value, list):
        for item in value:
            found += _paths_in(item)
    elif isinstance(value, str) and value.startswith("/") and Path(value).suffix:
        found.append(value)
    return found


class TestArrival:
    def test_no_path_that_is_not_on_this_computer_survives(self, wire):
        answer = served_makes.arrive("generate_coaster", _served_answer())
        assert "output_stl" not in answer and "scad_path" not in answer
        dead = [
            p for p in _paths_in({k: v for k, v in answer.items() if k != "artifact"})
            if not Path(p).exists()
        ]
        assert dead == []
        block = answer["made_on_kiln_servers"]
        assert block["on_this_computer"] is False
        assert block["artifact_token"] == TOKEN
        assert block["server_paths_removed"] == ["output_stl", "scad_path"]
        assert f'keep_design(artifact_token="{TOKEN}")' in block["what_to_do"]

    def test_the_look_is_fetched_for_the_stage_and_never_named(self, wire):
        answer = served_makes.arrive("generate_coaster", _served_answer())
        look = served_makes.arrival_path(TOKEN)
        assert look and Path(look).read_bytes() == LOOK_BYTES
        # Showing is free; the file a person can slice arrives with a keep.
        assert look not in json.dumps(answer)
        assert Path(look).parent.name == "arrivals"

    def test_the_allowance_is_said_from_the_manifest_or_not_at_all(self, wire):
        metered = served_makes.arrive(
            "generate_coaster",
            _served_answer(),
            allowance={"bucket": "generator", "limit": 3, "period": "month", "noun": "coasters"},
        )
        assert "keep" in metered["made_on_kiln_servers"]
        assert "monthly allowance" in metered["made_on_kiln_servers"]["keep"]
        unmetered = served_makes.arrive("design_session", _served_answer())
        assert unmetered["made_on_kiln_servers"]["keep"] == "Keeping it costs nothing extra."

    @pytest.mark.parametrize(
        "answer",
        [
            {"status": "error", "error": "no"},
            {"success": False, "artifact": {"artifact_token": TOKEN}},
            {"status": "success", "screw": "M3"},
            {"status": "success", "artifact": {"artifact_token": "../../etc"}},
            "not a dict",
        ],
    )
    def test_an_answer_that_made_nothing_is_untouched(self, wire, answer):
        calls, _ = wire
        before = json.dumps(answer, sort_keys=True)
        assert json.dumps(served_makes.arrive("recommend_hole", answer), sort_keys=True) == before
        assert calls == []

    def test_a_look_that_cannot_be_fetched_still_tells_the_truth(self, wire):
        _calls, answers = wire
        del answers[("GET", f"/api/artifact/{TOKEN}")]
        answer = served_makes.arrive("generate_coaster", _served_answer())
        assert "output_stl" not in answer
        assert served_makes.arrival_path(TOKEN) is None
        assert answer["made_on_kiln_servers"]["artifact_token"] == TOKEN

    def test_a_path_that_really_is_on_this_computer_is_kept(self, wire, tmp_path):
        local = tmp_path / "mine.stl"
        local.write_bytes(LOOK_BYTES)
        answer = served_makes.arrive("generate_coaster", _served_answer(source_model=str(local)))
        assert answer["source_model"] == str(local)


class TestKeep:
    def test_a_keep_saves_the_full_file_and_says_what_it_cost(self, wire):
        served_makes.arrive("generate_coaster", _served_answer())
        kept = served_makes.keep(TOKEN)
        assert kept["success"] is True and kept["kept"] is True
        path = Path(kept["mesh_path"])
        assert path.read_bytes() == FULL_BYTES
        assert path.parent == served_makes.kept_dir() and path.name.startswith("coaster-")
        assert kept["keep"] == {"used": 1, "limit": 3, "remaining": 2, "allowance": "generator"}
        assert kept["made_by"] == "generate_coaster"
        # The stage now shows the kept copy for this make.
        assert served_makes.arrival_path(TOKEN) == str(path)
        assert served_makes.token_for_path(str(path)) == TOKEN

    def test_a_second_keep_charges_nothing_and_asks_nobody(self, wire):
        calls, _ = wire
        served_makes.arrive("generate_coaster", _served_answer())
        first = served_makes.keep(TOKEN)
        posts = [c for c in calls if c[0] == "POST"]
        again = served_makes.keep(TOKEN)
        assert again["mesh_path"] == first["mesh_path"]
        assert [c for c in calls if c[0] == "POST"] == posts

    def test_a_spent_allowance_saves_nothing(self, wire):
        """The server refuses the keep: its own words reach the person, and
        no file lands here."""
        _calls, answers = wire
        wall = {
            "success": False,
            "code": "QUOTA_EXCEEDED",
            "error": "You've used your 3 free coasters this month.",
            "upgrade_url": "https://kiln3d.com/pricing?src=agent&tool=keep_design",
        }
        answers[("POST", f"/api/artifact/{TOKEN}/keep")] = _Response(status=429, body=wall)
        served_makes.arrive("generate_coaster", _served_answer())
        refused = served_makes.keep(TOKEN)
        assert refused.get("success") is False
        assert "3 free coasters" in json.dumps(refused)
        assert "mesh_path" not in refused
        assert not served_makes.kept_dir().exists() or not any(served_makes.kept_dir().iterdir())

    def test_a_make_the_servers_no_longer_hold(self, wire):
        _calls, answers = wire
        del answers[("POST", f"/api/artifact/{TOKEN}/keep")]
        refused = served_makes.keep(TOKEN)
        assert refused["code"] == "MAKE_NO_LONGER_HELD"
        assert "Make it again" in refused["error"]

    def test_signed_out_is_told_to_sign_in(self, wire, monkeypatch):
        monkeypatch.setattr(served_makes, "_bearer", lambda: "")
        refused = served_makes.keep(TOKEN)
        assert refused["code"] == "KILN_ACCOUNT_NOT_PAIRED"
        assert refused["setup_hint"] == "kiln signin"

    @pytest.mark.parametrize("bad", ["", "../../../etc/passwd", "/tmp/x.stl", "short"])
    def test_only_a_token_is_taken(self, wire, bad):
        calls, _ = wire
        assert served_makes.keep(bad)["code"] == "INVALID_INPUT"
        assert calls == []


class TestSend:
    INPUTS = {"mesh": "input_path", "image": "image_path"}

    def test_a_model_on_this_computer_goes_up_first(self, wire, tmp_path):
        calls, answers = wire
        answers[("POST", "/api/view/mesh")] = _Response(
            body={"status": "success", "artifact_token": "UPLOADED-token-0123456789"}
        )
        model = tmp_path / "bracket.stl"
        model.write_bytes(FULL_BYTES)
        kwargs, refusal = served_makes.send_inputs(
            "apply_procedural_texture",
            {"input_path": str(model), "texture": "hexagons"},
            self.INPUTS,
        )
        assert refusal is None
        assert kwargs == {"texture": "hexagons", "source_artifact_token": "UPLOADED-token-0123456789"}
        method, path, sent = calls[-1]
        assert (method, path) == ("POST", "/api/view/mesh")
        assert sent["data"] == {"source": "1"}
        assert sent["headers"]["Authorization"] == "Bearer bearer-token"

    def test_a_make_on_the_servers_is_named_by_its_token(self, wire):
        calls, _ = wire
        kwargs, refusal = served_makes.send_inputs(
            "apply_procedural_texture", {"input_path": TOKEN, "texture": "hexagons"}, self.INPUTS
        )
        assert refusal is None
        assert kwargs == {"texture": "hexagons", "source_artifact_token": TOKEN}
        assert calls == []

    def test_a_kept_copy_is_named_by_its_token_too(self, wire):
        calls, _ = wire
        served_makes.arrive("generate_coaster", _served_answer())
        kept = served_makes.keep(TOKEN)
        before = len(calls)
        kwargs, refusal = served_makes.send_inputs(
            "apply_procedural_texture",
            {"input_path": kept["mesh_path"], "texture": "hexagons"},
            self.INPUTS,
        )
        assert refusal is None and kwargs["source_artifact_token"] == TOKEN
        assert len(calls) == before

    def test_an_image_on_this_computer_goes_up_first(self, wire, tmp_path):
        _calls, answers = wire
        answers[("POST", "/api/images/upload")] = _Response(
            body={"image_token": "IMAGE-token-0123456789abc"}
        )
        logo = tmp_path / "logo.png"
        logo.write_bytes(b"\x89PNG\r\n")
        kwargs, refusal = served_makes.send_inputs(
            "apply_image_texture", {"input_path": TOKEN, "image_path": str(logo)}, self.INPUTS
        )
        assert refusal is None
        assert kwargs == {"source_artifact_token": TOKEN, "image_token": "IMAGE-token-0123456789abc"}

    def test_a_refused_upload_stops_the_call_and_says_why(self, wire, tmp_path):
        _calls, answers = wire
        answers[("POST", "/api/view/mesh")] = _Response(
            status=415, body={"status": "error", "error": "That does not look like a 3D model."}
        )
        model = tmp_path / "bracket.stl"
        model.write_bytes(b"not a mesh")
        kwargs, refusal = served_makes.send_inputs(
            "apply_procedural_texture", {"input_path": str(model)}, self.INPUTS
        )
        assert refusal["code"] == "MODEL_NOT_SENT"
        assert "does not look like a 3D model" in refusal["error"]

    def test_text_that_is_not_a_file_is_left_alone(self, wire):
        calls, _ = wire
        given = {"content": "HELLO", "model_path": "/nowhere/missing.stl"}
        kwargs, refusal = served_makes.send_inputs(
            "decorate_surface", dict(given), {"mesh": "model_path", "image": "content"}
        )
        assert refusal is None and kwargs == given and calls == []

    def test_a_tool_with_no_file_inputs_is_untouched(self, wire):
        given = {"screw_name": "M3"}
        assert served_makes.send_inputs("recommend_hole", given, None) == (given, None)


class TestThroughTheRegisteredTools:
    """The stub and the keep verb as an agent calls them."""

    @pytest.fixture
    def stubs(self, monkeypatch, tmp_path):
        """Register the stubs from a small manifest on a bare tool registry."""
        import kiln.server as server

        manifest = {
            "categories": {},
            "tools": [
                {
                    "name": "generate_coaster",
                    "description": "Generate a coaster.",
                    "tier": "free",
                    "access": "free_metered",
                    "quota": {"bucket": "generator", "limit": 3, "period": "month", "noun": "coasters"},
                    "parameters": {"type": "object", "properties": {"shape": {"type": "string"}}},
                },
                {
                    "name": "apply_procedural_texture",
                    "description": "Texture a model.",
                    "tier": "free",
                    "parameters": {
                        "type": "object",
                        "properties": {"input_path": {"type": "string"}, "texture": {"type": "string"}},
                        "required": ["input_path", "texture"],
                    },
                    "inputs": {"mesh": "input_path", "image": "image_path"},
                },
                {
                    "name": "list_design_releases",
                    "description": "List releases.",
                    "tier": "pro",
                    "served": False,
                    "parameters": {"type": "object", "properties": {}},
                },
            ],
        }
        fake_server_file = tmp_path / "pkg" / "server.py"
        fake_server_file.parent.mkdir()
        (fake_server_file.parent / "pro_tool_manifest.json").write_text(json.dumps(manifest))
        monkeypatch.setattr(server, "__file__", str(fake_server_file))

        registered: dict[str, object] = {}

        class _Registry:
            def tool(self, *args, **kwargs):
                def register(fn):
                    registered[fn.__name__] = fn
                    return fn

                return register

        server._register_pro_tool_stubs(_Registry())
        return server, registered

    def test_a_tool_the_servers_refuse_is_not_listed(self, stubs):
        _server, registered = stubs
        assert "list_design_releases" not in registered
        assert {"generate_coaster", "apply_procedural_texture", "keep_design"} <= set(registered)

    def test_make_then_keep(self, stubs, wire, monkeypatch):
        server, registered = stubs
        monkeypatch.setattr(server, "_pro_api_call", lambda name, **kw: _served_answer())
        made = registered["generate_coaster"](shape="round")
        assert "output_stl" not in made
        token = made["made_on_kiln_servers"]["artifact_token"]
        assert "3 coasters a month" in made["made_on_kiln_servers"]["keep"]

        from kiln import local_stage

        class _Result:
            content = [type("Block", (), {"text": json.dumps(made)})()]
            structuredContent = None
            isError = False

        assert local_stage.token_for_call_result(_Result()) == token
        assert local_stage.resolve(token) == served_makes.arrival_path(token)

        kept = registered["keep_design"](artifact_token=token)
        assert Path(kept["mesh_path"]).read_bytes() == FULL_BYTES

    def test_changing_a_served_make_sends_its_token_not_a_path(self, stubs, wire, monkeypatch):
        server, registered = stubs
        sent: dict = {}

        def forwarded(name, **kwargs):
            sent.update(kwargs)
            return _served_answer()

        monkeypatch.setattr(server, "_pro_api_call", forwarded)
        registered["apply_procedural_texture"](input_path=TOKEN, texture="hexagons")
        assert sent == {"texture": "hexagons", "source_artifact_token": TOKEN}


class TestAHeavyMakeRunsAsAJob:
    """A texture over a whole model runs for minutes on Kiln's servers,
    longer than one request stays open.  The stub asks for a job and polls
    for the answer.  (2026-10-03: a marble texture on a coaster ran 350
    seconds on one open request and came back "Kiln's servers didn't
    answer".)"""

    @pytest.fixture
    def served(self, monkeypatch, tmp_path):
        import io
        import urllib.error

        import kiln.server as server

        monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
        monkeypatch.setenv("KILN_LICENSE_KEY", "kiln_test_key")
        monkeypatch.setattr(server, "_SERVED_JOB_POLL_S", 0.0)
        requests: list[tuple[str, str, dict]] = []
        script: list[tuple[int, dict]] = []

        class _Resp:
            def __init__(self, status, body):
                self.status = status
                self._body = json.dumps(body).encode()

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(req, timeout=None):
            requests.append((req.get_method(), req.full_url, dict(req.header_items())))
            status, body = script.pop(0)
            if status >= 400:
                raise urllib.error.HTTPError(
                    req.full_url, status, "error", {}, io.BytesIO(json.dumps(body).encode())
                )
            return _Resp(status, body)

        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        return server, requests, script

    def test_an_accepted_job_is_polled_on_its_own_machine_until_it_answers(self, served):
        server, requests, script = served
        script += [
            (202, {"status": "accepted", "job_id": "j1", "machine_id": "m-7", "poll": "/api/tools/jobs/j1"}),
            (200, {"status": "running", "tool": "apply_procedural_texture", "elapsed_s": 3.0}),
            (200, {"status": "running", "tool": "apply_procedural_texture", "elapsed_s": 6.0}),
            (200, {"status": "success", "message": "Textured."}),
        ]
        out = server._pro_api_call("apply_procedural_texture", texture="marble")
        assert out == {"status": "success", "message": "Textured."}
        submit, *polls = requests
        assert submit[0] == "POST" and submit[2].get("X-kiln-tool-async") == "1"
        assert [p[0] for p in polls] == ["GET"] * 3
        assert all(p[1].endswith("/api/tools/jobs/j1") for p in polls)
        assert all(p[2].get("Fly-force-instance-id") == "m-7" for p in polls)
        assert all("X-kiln-tool-async" not in p[2] for p in polls)

    def test_a_tool_the_servers_answer_at_once_is_returned_as_is(self, served):
        server, requests, script = served
        script.append((200, {"status": "success", "hole_mm": 3.4}))
        assert server._pro_api_call("recommend_hole", screw_name="M3") == {
            "status": "success",
            "hole_mm": 3.4,
        }
        assert len(requests) == 1

    def test_a_job_that_ends_in_a_refusal_keeps_the_servers_own_words(self, served):
        server, _requests, script = served
        script += [
            (202, {"status": "accepted", "job_id": "j2", "machine_id": "m-7", "poll": "/api/tools/jobs/j2"}),
            (504, {"status": "error", "code": "JOB_TIMEOUT",
                   "error": "That took longer than Kiln's servers allow, so it was stopped. Nothing was charged."}),
        ]
        out = server._pro_api_call("apply_procedural_texture", texture="marble")
        assert out.get("success") is False or out.get("status") == "error"
        assert "Nothing was charged" in json.dumps(out)

    def test_a_job_lost_to_a_restart_is_said_plainly(self, served):
        server, _requests, script = served
        script += [
            (202, {"status": "accepted", "job_id": "j3", "machine_id": "m-7", "poll": "/api/tools/jobs/j3"}),
            (404, {"status": "error", "code": "JOB_NOT_FOUND",
                   "error": "That job isn't here — it may have finished long ago, or the machine restarted. Apply again."}),
        ]
        out = server._pro_api_call("apply_procedural_texture", texture="marble")
        assert "Apply again" in json.dumps(out, ensure_ascii=False)


class TestAJobIsWaitedForPatiently:
    """Measured on the live servers 2026-10-03: a poll timed out while the
    machine was busy with the job itself, and a sign-in token expired during
    a nine-minute make.  Neither is the job failing."""

    @pytest.fixture
    def served(self, monkeypatch, tmp_path):
        import io
        import urllib.error

        import kiln.server as server
        from kiln import auth_session

        monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
        monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
        monkeypatch.setattr(server, "_SERVED_JOB_POLL_S", 0.0)
        tokens = iter(["token-1", "token-1", "token-2", "token-2", "token-2"])
        monkeypatch.setattr(
            auth_session,
            "resolve_api_bearer",
            lambda *a, **k: auth_session.ApiBearer(token=next(tokens), state="live"),
        )
        requests: list[dict] = []
        script: list = []

        class _Resp:
            status = 200

            def __init__(self, body):
                self._body = json.dumps(body).encode()

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(req, timeout=None):
            requests.append(dict(req.header_items()))
            step = script.pop(0)
            if isinstance(step, Exception):
                raise step
            status, body = step
            if status >= 400:
                raise urllib.error.HTTPError(
                    req.full_url, status, "error", {}, io.BytesIO(json.dumps(body).encode())
                )
            resp = _Resp(body)
            resp.status = status
            return resp

        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        return server, requests, script

    def test_a_poll_that_gets_no_answer_is_asked_again(self, served):
        server, requests, script = served
        script += [
            (202, {"status": "accepted", "job_id": "j1", "machine_id": "m", "poll": "/api/tools/jobs/j1"}),
            TimeoutError("timed out"),
            (502, {}),
            (200, {"status": "running"}),
            (200, {"status": "success", "message": "Textured."}),
        ]
        assert server._pro_api_call("apply_procedural_texture", texture="marble") == {
            "status": "success",
            "message": "Textured.",
        }
        # Each poll carried the sign-in as it stood at that moment.
        assert [r["Authorization"] for r in requests] == [
            "Bearer token-1", "Bearer token-1", "Bearer token-2", "Bearer token-2", "Bearer token-2",
        ]
