"""A served tool that needs more than one file can be run from a plain install.

``kiln.served_makes`` could send exactly one model and one image.  A tool
that reads a print's G-code, two models, a list of parts or a PDF could not
be handed them, so Kiln's servers marked it not served and this install
listed no stub for it: on 2026-10-04 that was seventeen tools, the paid
mid-print edits among them.

These cover the two halves on this side, with only the network stood in
for: every file a call names goes up (a G-code compressed, an OBJ with the
files it needs beside it), and every file the answer hands over lands on
this computer where the call asked for it, with a real path in the answer.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from kiln import served_makes

GCODE = b"; sliced\n;LAYER_CHANGE\nG28\nG1 X10 Y10 E1.5 F1800\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
STL = b"solid part\nendsolid part\n"


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


@pytest.fixture(autouse=True)
def this_computer(monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.setattr(served_makes, "_bearer", lambda: "bearer-token")
    return tmp_path


@pytest.fixture
def wire(monkeypatch):
    """Kiln's servers: every upload is taken and given a token; downloads
    answer from a table."""
    import httpx

    uploads: list[dict] = []
    downloads: dict[str, _Response] = {}

    def post(url, **kwargs):
        path = url.split("api.kiln3d.com", 1)[-1]
        name, body, _type = kwargs["files"]["file"]
        data = body if isinstance(body, bytes) else body.read()
        uploads.append({"path": path, "name": name, "data": data, "headers": kwargs["headers"]})
        if path != "/api/tool-inputs":
            return _Response(status=404, body={"error": "not_found"})
        return _Response(body={"status": "success", "file_token": f"file-token-{len(uploads):08d}xxxxxx"})

    def get(url, **kwargs):
        path = url.split("api.kiln3d.com", 1)[-1]
        return downloads.get(path, _Response(status=404, body={"error": "not_found"}))

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)
    return uploads, downloads


def _file(folder: Path, name: str, data: bytes) -> str:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


MID_PRINT = {
    "files": {"decoration": "inline", "gcode_path": "one", "source_3mf_path": "one"},
    "outputs": {"output_dir": "folder"},
    "printer": "printer_name",
}


class TestSend:
    def test_a_gcode_goes_up_compressed_and_its_parameter_becomes_a_token(self, wire, tmp_path):
        uploads, _ = wire
        gcode = _file(tmp_path, "benchy.gcode", GCODE * 200)
        trip: dict = {}
        kwargs, refusal = served_makes.send_inputs(
            "decorate_during_print",
            {"decoration": "kiln logo", "gcode_path": gcode, "current_layer": 12},
            MID_PRINT, trip,
        )
        assert refusal is None
        assert kwargs == {
            "decoration": "kiln logo", "current_layer": 12,
            "file_tokens": {"gcode_path": {"token": "file-token-00000001xxxxxx", "name": "benchy.gcode"}},
        }
        assert [u["path"] for u in uploads] == ["/api/tool-inputs"]
        assert gzip.decompress(uploads[0]["data"]) == GCODE * 200
        assert len(uploads[0]["data"]) < len(GCODE * 200) / 4
        assert uploads[0]["headers"]["Authorization"] == "Bearer bearer-token"
        assert trip["files_sent"] == 1

    @pytest.mark.parametrize(
        ("said", "sent"),
        [
            ("photo:{path}", "photo:{file}"),
            ("logo: {path}", "logo:{file}"),
            ("{path}", "{file}"),
        ],
    )
    def test_a_file_named_in_words_goes_up_and_keeps_its_words(self, wire, tmp_path, said, sent):
        uploads, _ = wire
        mark = _file(tmp_path, "my mark.png", PNG)
        kwargs, refusal = served_makes.send_inputs(
            "decorate_during_print", {"decoration": said.replace("{path}", mark)}, MID_PRINT,
        )
        assert refusal is None
        assert kwargs["decoration"] == sent
        assert kwargs["file_tokens"]["decoration"]["name"] == "my mark.png"
        assert uploads[0]["data"] == PNG

    @pytest.mark.parametrize("words", ["kiln logo", "make the rest red", "texture:alligator", "ai:tribal"])
    def test_words_that_name_no_file_send_nothing(self, wire, words):
        uploads, _ = wire
        kwargs, refusal = served_makes.send_inputs(
            "decorate_during_print", {"decoration": words}, MID_PRINT,
        )
        assert refusal is None and kwargs == {"decoration": words} and uploads == []

    def test_a_list_of_parts_goes_up_in_order(self, wire, tmp_path):
        uploads, _ = wire
        parts = [_file(tmp_path, f"{n}.stl", STL + n.encode()) for n in ("left", "right", "base")]
        kwargs, refusal = served_makes.send_inputs(
            "advise_kit_scale", {"part_paths": parts, "bed_x_mm": 256.0},
            {"files": {"part_paths": "many"}},
        )
        assert refusal is None
        assert [e["name"] for e in kwargs["file_tokens"]["part_paths"]] == [
            "left.stl", "right.stl", "base.stl",
        ]
        assert "part_paths" not in kwargs and kwargs["bed_x_mm"] == 256.0
        assert [u["name"] for u in uploads] == ["left.stl", "right.stl", "base.stl"]

    def test_one_part_handed_to_a_list_parameter_is_still_a_list(self, wire, tmp_path):
        part = _file(tmp_path, "only.stl", STL)
        kwargs, _ = served_makes.send_inputs(
            "advise_kit_scale", {"part_paths": part}, {"files": {"part_paths": "many"}},
        )
        assert isinstance(kwargs["file_tokens"]["part_paths"], list)

    def test_two_models_each_fill_their_own_parameter(self, wire, tmp_path):
        before = _file(tmp_path, "v1.stl", STL)
        after = _file(tmp_path, "v2.stl", STL + b" ")
        kwargs, refusal = served_makes.send_inputs(
            "visual_diff_meshes",
            {"before_mesh_path": before, "after_mesh_path": after},
            {"files": {"before_mesh_path": "one", "after_mesh_path": "one"}},
        )
        assert refusal is None
        tokens = kwargs["file_tokens"]
        assert tokens["before_mesh_path"]["name"] == "v1.stl"
        assert tokens["after_mesh_path"]["name"] == "v2.stl"
        assert tokens["before_mesh_path"]["token"] != tokens["after_mesh_path"]["token"]

    def test_the_same_file_named_twice_goes_up_once(self, wire, tmp_path):
        uploads, _ = wire
        part = _file(tmp_path, "a.stl", STL)
        served_makes.send_inputs(
            "visual_diff_meshes", {"before_mesh_path": part, "after_mesh_path": part},
            {"files": {"before_mesh_path": "one", "after_mesh_path": "one"}},
        )
        assert len(uploads) == 1

    def test_a_make_still_on_the_servers_is_named_by_its_token(self, wire):
        uploads, _ = wire
        token = "QzN57tLRbyH2N-ZAHsJrK3vJX_k51thF"
        kwargs, refusal = served_makes.send_inputs(
            "visual_diff_meshes", {"before_mesh_path": token},
            {"files": {"before_mesh_path": "one"}},
        )
        assert refusal is None and uploads == []
        assert kwargs["file_tokens"] == {"before_mesh_path": {"token": token}}

    def test_an_obj_takes_its_materials_and_its_picture_with_it(self, wire, tmp_path):
        """An OBJ's colours are in the files it names; sent alone it is a
        grey shape, and a tool that reads its texture has nothing to read."""
        uploads, _ = wire
        _file(tmp_path, "model/skin.png", PNG)
        _file(tmp_path, "model/part.mtl", b"newmtl skin\nmap_Kd skin.png\n")
        _file(tmp_path, "model/unrelated.mtl", b"newmtl other\n")
        obj = _file(tmp_path, "model/part.obj", b"mtllib part.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
        kwargs, refusal = served_makes.send_inputs(
            "auto_multicolor_from_texture", {"obj_path": obj}, {"files": {"obj_path": "one"}},
        )
        assert refusal is None
        entry = kwargs["file_tokens"]["obj_path"]
        assert entry["name"] == "part.obj"
        assert [e["name"] for e in entry["with"]] == ["part.mtl", "skin.png"]
        assert sorted(u["name"] for u in uploads) == ["part.mtl", "part.obj", "skin.png"]

    def test_an_obj_that_names_files_elsewhere_takes_nothing_from_elsewhere(self, wire, tmp_path):
        uploads, _ = wire
        _file(tmp_path, "secrets/keys.mtl", b"newmtl x\n")
        obj = _file(tmp_path, "model/part.obj", b"mtllib ../secrets/keys.mtl\nv 0 0 0\nf 1 1 1\n")
        kwargs, _ = served_makes.send_inputs(
            "auto_multicolor_from_texture", {"obj_path": obj}, {"files": {"obj_path": "one"}},
        )
        assert "with" not in kwargs["file_tokens"]["obj_path"]
        assert [u["name"] for u in uploads] == ["part.obj"]

    def test_a_file_that_is_not_there_is_said_here_not_by_the_servers(self, wire, tmp_path):
        uploads, _ = wire
        _kwargs, refusal = served_makes.send_inputs(
            "decorate_during_print", {"gcode_path": str(tmp_path / "missing.gcode")}, MID_PRINT,
        )
        assert refusal["code"] == "FILE_NOT_FOUND" and "missing.gcode" in refusal["error"]
        assert uploads == []

    def test_a_kind_of_file_the_servers_do_not_take_is_said_plainly(self, wire, tmp_path):
        ini = _file(tmp_path, "profile.ini", b"[print]\n")
        _kwargs, refusal = served_makes.send_inputs(
            "decorate_during_print", {"gcode_path": ini}, MID_PRINT,
        )
        assert refusal["code"] == "FILE_KIND_NOT_SENT" and ".ini" in refusal["error"]

    def test_a_refused_upload_stops_the_call_with_the_servers_words(self, wire, tmp_path, monkeypatch):
        import httpx

        monkeypatch.setattr(
            httpx, "post",
            lambda url, **kw: _Response(status=413, body={"error": "That file is over 64 MB."}),
        )
        _kwargs, refusal = served_makes.send_inputs(
            "decorate_during_print", {"gcode_path": _file(tmp_path, "a.gcode", GCODE)}, MID_PRINT,
        )
        assert refusal["code"] == "FILE_NOT_SENT" and "over 64 MB" in refusal["error"]

    def test_a_gcode_past_the_ceiling_is_not_read_into_memory_to_find_out(self, wire, tmp_path, monkeypatch):
        uploads, _ = wire
        monkeypatch.setattr(served_makes, "MAX_GCODE_BYTES", 10)
        _kwargs, refusal = served_makes.send_inputs(
            "decorate_during_print", {"gcode_path": _file(tmp_path, "a.gcode", GCODE)}, MID_PRINT,
        )
        assert refusal["code"] == "FILE_NOT_SENT" and uploads == []

    def test_where_the_call_wanted_its_output_stays_on_this_computer(self, wire, tmp_path):
        trip: dict = {}
        kwargs, _ = served_makes.send_inputs(
            "embed_manual_in_3mf",
            {"threemf_path": _file(tmp_path, "b.3mf", b"PK\x03\x04"), "output_path": str(tmp_path / "out" / "bracket.3mf")},
            {"files": {"threemf_path": "one"}, "outputs": {"output_path": "file"}},
            trip,
        )
        assert "output_path" not in kwargs  # a place the servers cannot write
        assert kwargs["output_names"] == {"output_path": "bracket.3mf"}
        assert trip["output_files"] == {"output_path": str(tmp_path / "out" / "bracket.3mf")}

        trip = {}
        kwargs, _ = served_makes.send_inputs(
            "decorate_during_print", {"output_dir": str(tmp_path / "resume")}, MID_PRINT, trip,
        )
        assert "output_dir" not in kwargs and "output_names" not in kwargs
        assert trip["output_folders"] == {"output_dir": str(tmp_path / "resume")}


def _edit_answer() -> dict:
    return {
        "status": "ok",
        "session_id": "20261004_120000_abc123",
        "transformed_resume_path": "/tmp/kiln_decorate_x/transformed_resume.3mf",
        "transformed_upload_path": "/tmp/kiln_decorate_x/transformed_resume.3mf",
        "original_upload_path": "/tmp/kiln_decorate_x/original_resume.3mf",
        "carved_gcode_path": "/tmp/kiln_decorate_x/decorated.gcode",
        "engine_output_dir": "/tmp/kiln_decorate_x",
        "provenance": {"transformed_upload_path": "/tmp/kiln_decorate_x/transformed_resume.3mf"},
        "required_start_print_args": {"resume_from_paused": True},
        "files": [
            {"at": ["transformed_resume_path"], "filename": "transformed_resume.3mf",
             "format": "3mf", "artifact_token": "EDITED-token-0123456789", "expires_in": 1800},
            {"at": ["original_upload_path"], "filename": "original_resume.3mf",
             "format": "3mf", "artifact_token": "PLAIN-token-01234567890", "expires_in": 1800},
            {"at": ["carved_gcode_path"], "filename": "decorated.gcode",
             "format": "gcode", "artifact_token": "CARVED-token-0123456789", "expires_in": 1800},
        ],
    }


def _paths_in(value) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            found += _paths_in(item)
    elif isinstance(value, list):
        for item in value:
            found += _paths_in(item)
    elif isinstance(value, str) and value.startswith("/"):
        found.append(value)
    return found


class TestBring:
    @pytest.fixture
    def edit(self, wire):
        _, downloads = wire
        downloads["/api/artifact/EDITED-token-0123456789"] = _Response(content=b"PK edited")
        downloads["/api/artifact/PLAIN-token-01234567890"] = _Response(content=b"PK plain")
        downloads["/api/artifact/CARVED-token-0123456789"] = _Response(content=gzip.compress(GCODE))
        return _edit_answer()

    def test_both_resume_files_are_on_this_computer_and_the_answer_says_where(self, edit):
        answer = served_makes.arrive("decorate_during_print", edit)
        edited, plain = answer["transformed_upload_path"], answer["original_upload_path"]
        assert Path(edited).read_bytes() == b"PK edited"
        assert Path(plain).read_bytes() == b"PK plain"
        assert Path(edited).parent == served_makes.files_dir()
        # The same server path, wherever the answer repeated it.
        assert answer["transformed_resume_path"] == edited
        assert answer["provenance"]["transformed_upload_path"] == edited
        # A G-code comes down compressed and is saved as plain G-code.
        assert Path(answer["carved_gcode_path"]).read_bytes() == GCODE
        # Nothing in the answer names a place that is not on this computer.
        assert all(Path(p).exists() for p in _paths_in(answer)), _paths_in(answer)
        assert "engine_output_dir" not in answer
        assert answer["required_start_print_args"] == {"resume_from_paused": True}
        assert all(f["on_this_computer"] and "artifact_token" not in f for f in answer["files"])

    def test_files_go_into_the_folder_the_call_named(self, edit, tmp_path):
        trip = {"output_folders": {"output_dir": str(tmp_path / "resume")}}
        answer = served_makes.arrive("decorate_during_print", edit, trip=trip)
        assert answer["transformed_upload_path"] == str(tmp_path / "resume" / "transformed_resume.3mf")
        assert (tmp_path / "resume" / "original_resume.3mf").read_bytes() == b"PK plain"

    def test_an_output_lands_exactly_where_the_call_asked(self, wire, tmp_path):
        _, downloads = wire
        downloads["/api/artifact/OUTPUT-token-0123456789"] = _Response(content=b"PK with manual")
        wanted = tmp_path / "out" / "bracket.3mf"
        answer = served_makes.arrive(
            "embed_manual_in_3mf",
            {"success": True, "data": {"bytes": 14},
             "files": [{"param": "output_path", "filename": "bracket.3mf", "format": "3mf",
                        "artifact_token": "OUTPUT-token-0123456789"}]},
            trip={"output_files": {"output_path": str(wanted)}},
        )
        assert wanted.read_bytes() == b"PK with manual"
        assert answer["output_path"] == str(wanted)

    def test_a_part_in_a_list_that_did_not_arrive_is_a_name_not_a_path(self, wire):
        answer = served_makes.arrive("separate_overlapping_parts", {
            "success": True,
            "parts": [{"name": "a", "output": "/tmp/k/a_cut.stl"}],
            "pages": ["/tmp/k/page-1.png", "/tmp/k/page-2.png"],
        })
        assert "output" not in answer["parts"][0]
        assert answer["pages"] == ["page-1.png", "page-2.png"]

    def test_parts_in_a_list_each_land(self, wire):
        _, downloads = wire
        downloads["/api/artifact/PART-A-token-0123456789"] = _Response(content=STL)
        downloads["/api/artifact/PART-B-token-0123456789"] = _Response(content=STL + b" ")
        answer = served_makes.arrive("separate_overlapping_parts", {
            "success": True,
            "parts": [{"name": "a", "output": "/tmp/k/a_cut.stl"}, {"name": "b", "output": "/tmp/k/b_cut.stl"}],
            "files": [
                {"at": ["parts", 0, "output"], "filename": "a_cut.stl", "format": "stl",
                 "artifact_token": "PART-A-token-0123456789"},
                {"at": ["parts", 1, "output"], "filename": "b_cut.stl", "format": "stl",
                 "artifact_token": "PART-B-token-0123456789"},
            ],
        })
        assert Path(answer["parts"][0]["output"]).read_bytes() == STL
        assert Path(answer["parts"][1]["output"]).read_bytes() == STL + b" "

    def test_a_file_that_cannot_be_fetched_is_not_named_as_one(self, wire):
        answer = served_makes.arrive("decorate_during_print", _edit_answer())
        assert all(f["on_this_computer"] is False for f in answer["files"])
        # Not at the top of the answer, and not repeated further down it.
        assert _paths_in({k: v for k, v in answer.items() if k != "files"}) == []
        assert "transformed_upload_path" not in answer["provenance"]
        assert answer["files_on_kiln_servers"]["on_this_computer"] is False

    def test_the_servers_name_for_a_file_cannot_leave_its_folder(self, wire):
        _, downloads = wire
        downloads["/api/artifact/EVIL-token-012345678901"] = _Response(content=b"x")
        answer = served_makes.arrive("t", {
            "status": "ok", "out": "/tmp/k/x.stl",
            "files": [{"at": ["out"], "filename": "../../../../etc/cron.d/x.stl", "format": "stl",
                       "artifact_token": "EVIL-token-012345678901"}],
        })
        assert Path(answer["out"]).parent == served_makes.files_dir()

    @pytest.mark.parametrize(
        "entry",
        [
            {"at": ["out"], "format": "exe", "artifact_token": "EVIL-token-012345678901"},
            {"at": ["out"], "format": "stl", "artifact_token": "../../etc/passwd"},
            "not a dict",
        ],
    )
    def test_an_entry_that_is_not_a_file_fetches_nothing(self, wire, entry):
        _, downloads = wire
        downloads["/api/artifact/EVIL-token-012345678901"] = _Response(content=b"x")
        answer = served_makes.arrive("t", {"status": "ok", "out": "/tmp/k/x.stl", "files": [entry]})
        assert not served_makes.files_dir().exists()
        assert "out" not in answer

    def test_an_oversized_file_is_not_saved(self, edit, monkeypatch):
        monkeypatch.setattr(served_makes, "MAX_FILE_BYTES", 4)
        answer = served_makes.arrive("decorate_during_print", edit)
        assert all(f["on_this_computer"] is False for f in answer["files"])

    def test_signed_out_fetches_nothing(self, edit, monkeypatch):
        monkeypatch.setattr(served_makes, "_bearer", lambda: "")
        answer = served_makes.arrive("decorate_during_print", edit)
        assert not served_makes.files_dir().exists()
        assert all(f["on_this_computer"] is False for f in answer["files"])


class TestThroughTheStub:
    """The registered stub, as an agent calls it."""

    @pytest.fixture
    def stubs(self, monkeypatch, tmp_path):
        import kiln.server as server

        manifest = {
            "categories": {},
            "tools": [
                {
                    "name": "decorate_during_print",
                    "description": "Edit a paused print.",
                    "tier": "pro",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "decoration": {"type": "string"},
                            "gcode_path": {"type": "string"},
                            "current_layer": {"type": "integer"},
                            "printer_name": {"type": "string", "default": "default"},
                            "output_dir": {"type": "string", "default": ""},
                        },
                        "required": ["decoration", "gcode_path", "current_layer"],
                    },
                    "inputs": MID_PRINT,
                },
                {
                    "name": "add_feature_during_print",
                    "description": "Add a part to a paused print.",
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

    def test_a_mid_print_edit_end_to_end(self, stubs, wire, monkeypatch, tmp_path):
        server, registered = stubs
        _, downloads = wire
        downloads["/api/artifact/EDITED-token-0123456789"] = _Response(content=b"PK edited")
        downloads["/api/artifact/PLAIN-token-01234567890"] = _Response(content=b"PK plain")
        downloads["/api/artifact/CARVED-token-0123456789"] = _Response(content=gzip.compress(GCODE))
        monkeypatch.setattr(server, "_resolve_printer_model_live", lambda name=None: "bambu_a1")
        sent: dict = {}

        def forwarded(name, **kwargs):
            sent.update(kwargs)
            return _edit_answer()

        monkeypatch.setattr(server, "_pro_api_call", forwarded)
        assert "add_feature_during_print" not in registered

        answer = registered["decorate_during_print"](
            decoration=f"logo:{_file(tmp_path, 'mark.png', PNG)}",
            gcode_path=_file(tmp_path, "benchy.gcode", GCODE),
            current_layer=40,
            output_dir=str(tmp_path / "resume"),
        )

        # What went to the servers: tokens and words, the printer this
        # install has, a long wait -- and no path on this computer.
        assert sent["decoration"] == "logo:{file}"
        assert set(sent["file_tokens"]) == {"decoration", "gcode_path"}
        assert sent["printer_name"] == "bambu_a1"
        assert sent["current_layer"] == 40
        assert sent["_timeout"] == server._SERVED_MAKE_WAIT_S
        assert "output_dir" not in sent and "gcode_path" not in sent
        assert str(tmp_path) not in json.dumps({k: v for k, v in sent.items() if k != "_timeout"})

        # What came back: both resume files, in the folder that was asked for.
        assert Path(answer["transformed_upload_path"]) == tmp_path / "resume" / "transformed_resume.3mf"
        assert (tmp_path / "resume" / "original_resume.3mf").read_bytes() == b"PK plain"

    def test_a_printer_the_call_named_is_left_as_named(self, stubs, monkeypatch):
        server, _ = stubs
        monkeypatch.setattr(server, "_resolve_printer_model_live", lambda name=None: "bambu_a1")
        named = server._with_local_printer({"printer_name": "prusa_mk4"}, MID_PRINT)
        assert named == {"printer_name": "prusa_mk4"}

    @pytest.mark.parametrize("unnamed", [None, "", "default", "Default", "active"])
    def test_an_unnamed_printer_is_this_installs_own(self, stubs, monkeypatch, unnamed):
        server, _ = stubs
        monkeypatch.setattr(server, "_resolve_printer_model_live", lambda name=None: "bambu_a1")
        kwargs = {} if unnamed is None else {"printer_name": unnamed}
        assert server._with_local_printer(kwargs, MID_PRINT)["printer_name"] == "bambu_a1"

    def test_a_printer_with_no_known_model_is_named_by_how_it_connects(self, stubs, monkeypatch):
        server, _ = stubs
        monkeypatch.setattr(server, "_resolve_printer_model_live", lambda name=None: "")
        monkeypatch.setattr(server, "_PRINTER_HOST", "192.168.1.50")
        monkeypatch.setattr(server, "_PRINTER_TYPE", "moonraker")
        assert server._with_local_printer({}, MID_PRINT) == {"printer_name": "moonraker"}

    def test_an_install_with_no_printer_claims_none(self, stubs, monkeypatch):
        """The connection type has a built-in default.  Passed off as the
        user's printer it would get them a file built for a machine they do
        not have; left out, the servers ask."""
        server, _ = stubs
        monkeypatch.setattr(server, "_resolve_printer_model_live", lambda name=None: "")
        monkeypatch.setattr(server, "_PRINTER_HOST", "")
        monkeypatch.setattr(server, "_read_config_printers", lambda: {})
        assert server._with_local_printer({"printer_name": "default"}, MID_PRINT) == {
            "printer_name": "default",
        }

    def test_a_tool_that_builds_no_file_for_a_printer_is_untouched(self, stubs, monkeypatch):
        server, _ = stubs
        monkeypatch.setattr(server, "_resolve_printer_model_live", lambda name=None: "bambu_a1")
        assert server._with_local_printer({"printer_name": "default"}, {"mesh": "input_path"}) == {
            "printer_name": "default",
        }
        assert server._with_local_printer({"printer_name": "default"}, None) == {"printer_name": "default"}


def test_every_served_call_says_this_install_saves_files(monkeypatch):
    """The servers hand files back only to a caller that asks."""
    import urllib.request

    import kiln.server as server

    seen: dict = {}

    class _Resp:
        status = 200

        def read(self):
            return b'{"status": "ok"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        seen.update({k.lower(): v for k, v in req.header_items()})
        return _Resp()

    monkeypatch.setenv("KILN_LICENSE_KEY", "test-key")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    server._pro_api_call("compute_iso_fit", nominal=10)
    assert seen.get("x-kiln-result-files") == "1"
