"""A make that arrives from Kiln's servers arrives with its notes.

A part keeps small notes beside its file, the layer height a relief was
designed in among them, and Kiln's servers keep them with the make.  The
three doors that bring a make's file to this computer (keeping it, a file a
served tool hands over, and the bridge fetching a make to print) wrote the
file alone, so a relief made on the web and printed through this computer
was sliced at the profile's layer and printed fewer levels than it was cut
in.  Each now writes the notes beside the copy.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from kiln import served_makes
from kiln.slicer_layer import read as read_layer

TOKEN = "QzN57tLRbyH2N-ZAHsJrK3vJX_k51thF"
FULL_BYTES = b"solid full\n" + b"facet\n" * 40 + b"endsolid full\n"


def _layer_note(data: bytes) -> dict:
    return {
        "schema": 1,
        "file": hashlib.sha256(data).hexdigest()[:32],
        "layer": {"height_mm": 0.2, "levels": 8, "depth_mm": 1.6, "made_by": "decorate_surface"},
    }


class _Response:
    def __init__(self, status=200, content=b"", body=None, headers=None):
        self.status_code = status
        self.content = content
        self._body = body
        self.headers = {"content-type": "model/stl", **(headers or {})}

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
def servers(monkeypatch):
    """Kiln's servers: the make's bytes, its keep, and the notes kept with it."""
    import httpx

    notes = {".layer.json": _layer_note(FULL_BYTES), ".not_a_kiln_note.json": {"x": 1}}
    answers = {
        ("GET", f"/api/artifact/{TOKEN}"): _Response(content=FULL_BYTES),
        ("POST", f"/api/artifact/{TOKEN}/keep"): _Response(
            content=FULL_BYTES, headers={"content-disposition": 'attachment; filename="model.stl"'},
        ),
        ("GET", f"/api/artifact/{TOKEN}/notes"): _Response(body={"notes": notes}),
    }

    def _answer(method):
        def call(url, **kwargs):
            path = url.split("api.kiln3d.com", 1)[-1]
            return answers.get((method, path), _Response(status=404, body={"error": "not_found"}))

        return call

    monkeypatch.setattr(httpx, "get", _answer("GET"))
    monkeypatch.setattr(httpx, "post", _answer("POST"))
    monkeypatch.setattr(served_makes, "_api_base", lambda: "https://api.kiln3d.com")
    return answers


def test_a_kept_make_is_sliced_in_the_layer_it_was_designed_in(servers):
    kept = served_makes.keep(TOKEN)
    assert kept.get("success") is not False, kept
    path = served_makes._known(TOKEN)["kept"]
    assert Path(path).read_bytes() == FULL_BYTES
    layer = read_layer(path)
    assert layer is not None and layer.height_mm == 0.2
    assert not os.path.exists(path + ".not_a_kiln_note.json"), "only the notes public Kiln keeps"


def test_a_handed_over_model_arrives_with_its_notes(servers, tmp_path):
    answer = {
        "success": True,
        "output_path": "/srv/kiln_scratch/relief.stl",
        "files": [{"at": ["output_path"], "filename": "relief.stl", "format": "stl",
                   "artifact_token": TOKEN}],
    }
    served_makes._bring_files(answer, {"output_folders": {"out": str(tmp_path / "out")}})
    path = answer["output_path"]
    assert Path(path).read_bytes() == FULL_BYTES
    assert read_layer(path).height_mm == 0.2


def test_the_bridge_prints_a_make_in_the_layer_it_was_designed_in(servers, monkeypatch):
    from kiln import bridge_client

    class _Resp:
        def read(self):
            return FULL_BYTES

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(bridge_client.urllib.request, "urlopen", lambda request, timeout=0: _Resp())
    monkeypatch.setenv("KILN_API_URL", "https://api.kiln3d.com")
    path = bridge_client._default_artifact_fetcher(lambda: "bearer-token")(TOKEN)
    try:
        assert read_layer(path).height_mm == 0.2
    finally:
        for victim in (path, path + ".layer.json"):
            if os.path.exists(victim):
                os.unlink(victim)
        if os.path.isdir(os.path.dirname(path)) and os.path.basename(os.path.dirname(path)).startswith("kiln-relay-"):
            os.rmdir(os.path.dirname(path))


def test_a_note_is_never_written_through_a_link(servers, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text("untouched")
    model = tmp_path / "part.stl"
    model.write_bytes(FULL_BYTES)
    (tmp_path / "part.stl.layer.json").symlink_to(target)
    assert served_makes.lay_notes_beside(TOKEN, model) == []
    assert target.read_text() == "untouched"


def test_a_link_planted_while_the_note_is_written_takes_nothing(servers, tmp_path, monkeypatch):
    """A link that appears at the note's path after any look at it, as the
    note is being written, is refused by the open itself."""
    target = tmp_path / "elsewhere.json"
    target.write_text("untouched")
    model = tmp_path / "part.stl"
    model.write_bytes(FULL_BYTES)
    side = tmp_path / "part.stl.layer.json"
    real_dumps = json.dumps

    def dumps_then_plant(*args, **kwargs):
        text = real_dumps(*args, **kwargs)
        if not side.is_symlink():
            side.symlink_to(target)
        return text

    monkeypatch.setattr(json, "dumps", dumps_then_plant)
    assert served_makes.lay_notes_beside(TOKEN, model) == []
    assert target.read_text() == "untouched"


def test_the_bridge_keeps_its_copy_in_a_folder_of_its_own(servers, monkeypatch):
    from kiln import bridge_client

    class _Resp:
        def read(self):
            return FULL_BYTES

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(bridge_client.urllib.request, "urlopen", lambda request, timeout=0: _Resp())
    path = bridge_client._default_artifact_fetcher(lambda: "bearer-token")(TOKEN)
    folder = Path(path).parent
    try:
        assert folder.name.startswith("kiln-relay-")
        assert oct(folder.stat().st_mode & 0o777) == oct(0o700)
        assert sorted(p.name for p in folder.iterdir()) == ["model.stl", "model.stl.layer.json"]
    finally:
        for p in folder.iterdir():
            p.unlink()
        folder.rmdir()


def test_a_server_without_notes_leaves_the_copy_as_it_was(servers, tmp_path):
    servers.pop(("GET", f"/api/artifact/{TOKEN}/notes"))
    model = tmp_path / "part.stl"
    model.write_bytes(FULL_BYTES)
    assert served_makes.lay_notes_beside(TOKEN, model) == []
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("part")) == ["part.stl"]


def test_the_notes_written_are_the_ones_the_servers_kept(servers, tmp_path):
    model = tmp_path / "part.stl"
    model.write_bytes(FULL_BYTES)
    written = served_makes.lay_notes_beside(TOKEN, model)
    assert written == [str(model) + ".layer.json"]
    assert json.loads(Path(written[0]).read_text()) == _layer_note(FULL_BYTES)
