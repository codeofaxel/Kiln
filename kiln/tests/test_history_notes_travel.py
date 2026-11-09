"""The notes beside a design's model travel with it to the person's Kiln
account and back, and only the new or changed ones cross.

Kiln keeps small notes beside a model file, each bound to the file's bytes
(``kiln.asset_store._COMPANION_SUFFIXES``): the layer a relief was designed
in (``<model>.layer.json``) and the faces a carve made
(``<model>.decoration_faces.json``).  These tests walk the history records
(``kiln.history_vault``) and the courier (``kiln.history_courier``) against
a stand-in for Kiln's cloud routes:

* the records keep each library version's notes beside their own copy of
  its model, and follow a note changed in the library;
* a push carries a model's notes once, on the newest version naming it, and
  only those the account lacks or keeps differently; to a server that lists
  no notes, or has no "which models do you keep" door, it carries none, and
  it never carries past half of what one request may hold;
* a pull or clone onto a clean computer lays each note beside the model, one
  request a note, and a second pull fetches none; a note that cannot be had
  never fails the pull, and no recipe this computer keeps holds a note.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from kiln import history_courier as courier
from kiln.history_vault import HistoryVault
from kiln.slicer_layer import DesignedLayer, record
from tests.test_history_vault import _library

MESH_V1 = b"solid one\nfacet normal 0 0 1\nendsolid one\n"
MESH_V2 = b"solid two\nfacet normal 1 0 0\nendsolid two\n"
LAYER = ".layer.json"
FACES = ".decoration_faces.json"
NOTES = "mesh_notes_b64"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KILN_DESIGNS_DIR", raising=False)
    return tmp_path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _layer_beside(path: Path, height: float = 0.2) -> bytes:
    assert record(str(path), DesignedLayer(height_mm=height, levels=3, made_by="decorate_surface"))
    return Path(str(path) + LAYER).read_bytes()


def _carve(mesh: bytes, *, names: bytes | None = None) -> bytes:
    return json.dumps({
        "_meta": {"schema": "kiln.decoration_faces", "version": 1},
        "mesh_sha256": _sha(mesh if names is None else names),
        "face_indices": [3, 4, 5],
    }, separators=(",", ":")).encode()


def _vault_note(sha: str, suffix: str) -> bytes | None:
    vault = HistoryVault()
    try:
        mesh = vault.mesh_path(sha)
    finally:
        vault.close()
    side = Path(str(mesh) + suffix) if mesh is not None else None
    return side.read_bytes() if side is not None and side.is_file() else None


def _branch() -> dict:
    return {"branch_id": "mug-main", "design_id": "mug", "name": "main", "head_version_id": "v2",
            "parent_branch_id": None, "base_version_id": None, "created_at": 1.0, "status": "active",
            "annotation": "", "metadata": {}}


class _Account:
    """Kiln's cloud routes as the courier meets them; every call is kept.

    *notes* is what the account keeps beside each model, ``{sha: {suffix:
    bytes}}``; *lists_notes* False answers the stored question as a server
    that keeps no notes does; *stored_door* False is a server without it.
    """

    def __init__(self, models: dict[str, bytes] | None = None, *, notes=None, lists_notes=True,
                 stored_door=True, note_door=None, stored_refusal=None, recipe_extra=None) -> None:
        self.models = models or {}
        self.notes: dict[str, dict[str, bytes]] = notes if notes is not None else {}
        self.lists_notes, self.stored_door = lists_notes, stored_door
        self.note_door = note_door or {}
        self.stored_refusal = stored_refusal
        self.recipe_extra = recipe_extra or {}
        self.kept: set[str] = set()
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, tool, method, path, body=None, *, content=None, raw=False, timeout=None):
        self.calls.append((method, path, body))
        if path.endswith("/proven") or path.endswith("/proven/push"):
            return {"recipes": []}
        if path == "/api/cloud/meshes/stored":
            if self.stored_refusal is not None:
                return self.stored_refusal
            if not self.stored_door:
                return {"status": "error", "success": False, "code": "KILN_API_HTTP_ERROR", "http_status": 404}
            asked = [s for s in body["sha256s"]]
            all_models = self.kept | {_sha(m) for m in self.models.values()}
            out = {"stored": sorted(s for s in asked if s in all_models)}
            if self.lists_notes and body.get("notes") is True:
                out["notes"] = {
                    s: {suf: _sha(raw_) for suf, raw_ in self.notes.get(s, {}).items()}
                    for s in out["stored"] if self.notes.get(s)
                }
            return out
        if method == "PUT" and path.startswith("/api/cloud/meshes/"):
            self.kept.add(path.rsplit("/", 1)[1])
            return {"stored": True}
        if path.endswith("/branches/push"):
            return {"pushed_versions": [v["version_id"] for v in body["versions"]]}
        if method == "GET" and "/notes/" in path:
            sha, name = path.split("/api/cloud/meshes/", 1)[1].split("/notes/")
            said = self.note_door.get((sha, name))
            if said is not None:
                return said
            raw_ = self.notes.get(sha, {}).get(f".{name}.json")
            if raw_ is None:
                return {"status": "error", "success": False, "code": "MESH_NOTE_NOT_FOUND", "http_status": 404}
            return {"content": raw_}
        if method == "GET" and path.startswith("/api/cloud/meshes/"):
            return {"content": {_sha(m): m for m in self.models.values()}[path.rsplit("/", 1)[1]]}
        return self._answer(clone=path.endswith("/clone"), named=(body or {}).get("mesh_transport") == "by_sha256")

    def _answer(self, *, clone: bool, named: bool) -> dict:
        def recipe(vid: str, mesh: bytes) -> dict:
            out = {"name": "mug", "version_id": vid, "version": int(vid[1:]), "stl_path": f"{_sha(mesh)}.stl",
                   **self.recipe_extra.get(vid, {})}
            if not named:
                out["mesh_bytes_b64"] = base64.b64encode(mesh).decode("ascii")
            return out

        out = {
            "versions": [{"version_id": vid, "parent_version_id": None, "created_at": float(i),
                          **({"mesh_sha256": _sha(m)} if named else {})}
                         for i, (vid, m) in enumerate(self.models.items())],
            "memberships": [{"branch_id": "mug-main", "version_id": vid, "position": i, "added_at": 1.0}
                            for i, vid in enumerate(self.models)],
            "provenance_sidecars": [{"version_id": vid, "recipe_dict": recipe(vid, m)}
                                    for vid, m in self.models.items()],
        }
        if clone:
            out.update(design_id="mug", branches=[_branch()], releases=[])
        else:
            out["branch"] = _branch()
        if named:
            out["mesh_transport"] = "by_sha256"
        return out

    def asked(self) -> list[dict]:
        return [b for m, p, b in self.calls if p == "/api/cloud/meshes/stored"]

    def pushed(self) -> dict:
        return [b for m, p, b in self.calls if p.endswith("/branches/push")][-1]

    def note_gets(self) -> list[str]:
        return [p for m, p, _ in self.calls if m == "GET" and "/notes/" in p]


def _carried(body: dict) -> dict[str, dict[str, bytes]]:
    """The notes a push carries, decoded, by the version's place on the
    branch (``v1`` the first)."""
    return {
        f"v{i + 1}": {suf: base64.b64decode(b64) for suf, b64 in s["recipe_dict"][NOTES].items()}
        for i, s in enumerate(body["provenance_sidecars"]) if NOTES in s["recipe_dict"]
    }


# ---------------------------------------------------------------------------
# The records keep each version's notes beside their copy of its model
# ---------------------------------------------------------------------------


class TestTheRecordsKeepNotes:
    def test_a_library_versions_notes_are_kept_beside_the_records_copy_of_its_model(self, home):
        folder = _library(home, "mug", [MESH_V1, MESH_V2])
        layer = _layer_beside(folder / "v1.stl")
        (folder / ("v1.stl" + FACES)).write_bytes(_carve(MESH_V1))
        vault = HistoryVault()
        try:
            vault.sync_from_library("mug")
        finally:
            vault.close()
        assert _vault_note(_sha(MESH_V1), LAYER) == layer
        assert _vault_note(_sha(MESH_V1), FACES) == _carve(MESH_V1)
        assert _vault_note(_sha(MESH_V2), LAYER) is None

    def test_a_note_changed_in_the_library_after_the_version_was_recorded_is_followed(self, home):
        folder = _library(home, "mug", [MESH_V1])
        _layer_beside(folder / "v1.stl", 0.2)
        vault = HistoryVault()
        try:
            vault.sync_from_library("mug")
            changed = _layer_beside(folder / "v1.stl", 0.12)
            vault.sync_from_library("mug")
        finally:
            vault.close()
        assert _vault_note(_sha(MESH_V1), LAYER) == changed

    def test_a_note_naming_other_bytes_is_never_kept(self, home):
        folder = _library(home, "mug", [MESH_V1])
        (folder / ("v1.stl" + FACES)).write_bytes(_carve(MESH_V1, names=MESH_V2))
        vault = HistoryVault()
        try:
            vault.sync_from_library("mug")
        finally:
            vault.close()
        assert _vault_note(_sha(MESH_V1), FACES) is None


# ---------------------------------------------------------------------------
# The push
# ---------------------------------------------------------------------------


class TestThePush:
    def test_a_push_carries_each_models_notes_and_asks_for_the_listing(self, home, monkeypatch):
        folder = _library(home, "mug", [MESH_V1, MESH_V2])
        layer = _layer_beside(folder / "v1.stl")
        account = _Account()
        monkeypatch.setattr(courier, "_request", account)

        assert courier.push("cloud_push_branch", "mug")["status"] == "success"

        assert account.asked()[0]["notes"] is True
        assert _carried(account.pushed()) == {"v1": {LAYER: layer}}

    def test_a_second_push_of_an_unchanged_history_carries_no_notes(self, home, monkeypatch):
        folder = _library(home, "mug", [MESH_V1, MESH_V2])
        layer = _layer_beside(folder / "v1.stl")
        account = _Account(notes={_sha(MESH_V1): {LAYER: layer}})
        account.kept = {_sha(MESH_V1), _sha(MESH_V2)}
        monkeypatch.setattr(courier, "_request", account)

        assert courier.push("cloud_push_branch", "mug")["status"] == "success"

        assert _carried(account.pushed()) == {}

    def test_a_changed_layer_note_goes_up_alone(self, home, monkeypatch):
        folder = _library(home, "mug", [MESH_V1, MESH_V2])
        old = _layer_beside(folder / "v1.stl", 0.2)
        (folder / ("v1.stl" + FACES)).write_bytes(_carve(MESH_V1))
        account = _Account(notes={_sha(MESH_V1): {LAYER: old, FACES: _carve(MESH_V1)}})
        account.kept = {_sha(MESH_V1), _sha(MESH_V2)}
        monkeypatch.setattr(courier, "_request", account)
        assert courier.push("cloud_push_branch", "mug")["status"] == "success"
        assert _carried(account.pushed()) == {}

        new = _layer_beside(folder / "v1.stl", 0.12)
        assert courier.push("cloud_push_branch", "mug")["status"] == "success"

        assert _carried(account.pushed()) == {"v1": {LAYER: new}}

    def test_a_model_several_versions_name_carries_its_notes_once_on_the_newest(self, home, monkeypatch):
        folder = _library(home, "mug", [MESH_V1, MESH_V2, MESH_V1])
        layer = _layer_beside(folder / "v1.stl")
        _layer_beside(folder / "v3.stl")
        account = _Account()
        monkeypatch.setattr(courier, "_request", account)

        assert courier.push("cloud_push_branch", "mug")["status"] == "success"

        assert _carried(account.pushed()) == {"v3": {LAYER: layer}}

    def test_a_server_whose_answer_lists_no_notes_is_sent_none(self, home, monkeypatch):
        folder = _library(home, "mug", [MESH_V1])
        _layer_beside(folder / "v1.stl")
        account = _Account(lists_notes=False)
        monkeypatch.setattr(courier, "_request", account)

        assert courier.push("cloud_push_branch", "mug")["status"] == "success"

        assert NOTES not in json.dumps(account.pushed())

    def test_a_server_without_the_stored_door_is_sent_none(self, home, monkeypatch):
        folder = _library(home, "mug", [MESH_V1])
        _layer_beside(folder / "v1.stl")
        account = _Account(stored_door=False)
        monkeypatch.setattr(courier, "_request", account)

        assert courier.push("cloud_push_branch", "mug")["status"] == "success"

        body = account.pushed()
        assert "mesh_bytes_b64" in body["provenance_sidecars"][0]["recipe_dict"]
        assert NOTES not in json.dumps(body)

    def test_notes_never_take_a_push_past_half_of_what_one_request_may_carry(self, home, monkeypatch):
        folder = _library(home, "mug", [MESH_V1, MESH_V2])
        _layer_beside(folder / "v1.stl")
        newest = _layer_beside(folder / "v2.stl")
        room = len(LAYER) + len(base64.b64encode(newest)) + 4
        monkeypatch.setattr(courier, "CLOUD_REQUEST_LIMIT", 2 * room)
        account = _Account()
        monkeypatch.setattr(courier, "_request", account)

        assert courier.push("cloud_push_branch", "mug")["status"] == "success"

        assert _carried(account.pushed()) == {"v2": {LAYER: newest}}


# ---------------------------------------------------------------------------
# The pull and the clone
# ---------------------------------------------------------------------------


def _bring(how: str):
    return courier.pull("cloud_pull_branch", "mug") if how == "pull" else courier.clone("cloud_clone_design", "mug")


HOW = pytest.mark.parametrize("how", ["pull", "clone"])


def _library_note(home: Path, sha: str, suffix: str) -> bytes | None:
    copies = sorted((home / ".kiln" / "designs" / "mug").glob(f"mesh-{sha[:16]}.stl"))
    side = Path(str(copies[0]) + suffix) if copies else None
    return side.read_bytes() if side is not None and side.is_file() else None




class TestThePullAndTheClone:
    @HOW
    def test_a_clean_computer_gets_each_note_beside_the_model_one_request_a_note(self, home, monkeypatch, how):
        layer = json.dumps({"schema": 1, "file": _sha(MESH_V1)[:32],
                            "layer": {"height_mm": 0.2}}).encode()
        account = _Account({"v1": MESH_V1, "v2": MESH_V2},
                           notes={_sha(MESH_V1): {LAYER: layer, FACES: _carve(MESH_V1)}})
        monkeypatch.setattr(courier, "_request", account)

        assert _bring(how)["status"] == "success"

        assert sorted(account.note_gets()) == sorted([
            f"/api/cloud/meshes/{_sha(MESH_V1)}/notes/layer",
            f"/api/cloud/meshes/{_sha(MESH_V1)}/notes/decoration_faces",
        ])
        assert _vault_note(_sha(MESH_V1), LAYER) == layer
        assert _vault_note(_sha(MESH_V1), FACES) == _carve(MESH_V1)
        assert _library_note(home, _sha(MESH_V1), LAYER) == layer
        assert _library_note(home, _sha(MESH_V1), FACES) == _carve(MESH_V1)
        assert account.asked()[-1]["notes"] is True

    @HOW
    def test_a_second_pull_fetches_no_note(self, home, monkeypatch, how):
        account = _Account({"v1": MESH_V1}, notes={_sha(MESH_V1): {FACES: _carve(MESH_V1)}})
        monkeypatch.setattr(courier, "_request", account)
        assert _bring(how)["status"] == "success"
        assert len(account.note_gets()) == 1

        assert _bring(how)["status"] == "success"

        assert len(account.note_gets()) == 1

    def test_a_note_whose_door_fails_leaves_the_pull_whole_without_it(self, home, monkeypatch):
        layer = json.dumps({"schema": 1, "file": _sha(MESH_V1)[:32], "layer": {"height_mm": 0.2}}).encode()
        account = _Account(
            {"v1": MESH_V1}, notes={_sha(MESH_V1): {LAYER: layer, FACES: _carve(MESH_V1)}},
            note_door={(_sha(MESH_V1), "decoration_faces"): {
                "status": "error", "success": False, "code": "KILN_API_HTTP_ERROR", "http_status": 500,
            }},
        )
        monkeypatch.setattr(courier, "_request", account)

        answer = courier.pull("cloud_pull_branch", "mug")

        assert answer["status"] == "success", answer
        assert _vault_note(_sha(MESH_V1), LAYER) == layer
        assert _vault_note(_sha(MESH_V1), FACES) is None

    def test_a_note_naming_other_bytes_is_never_written(self, home, monkeypatch):
        account = _Account({"v1": MESH_V1}, notes={_sha(MESH_V1): {FACES: _carve(MESH_V1, names=MESH_V2)}})
        monkeypatch.setattr(courier, "_request", account)

        assert courier.pull("cloud_pull_branch", "mug")["status"] == "success"

        assert _vault_note(_sha(MESH_V1), FACES) is None
        assert _library_note(home, _sha(MESH_V1), FACES) is None

    def test_a_refused_listing_fetches_nothing_and_the_pull_goes_on(self, home, monkeypatch):
        account = _Account({"v1": MESH_V1}, notes={_sha(MESH_V1): {FACES: _carve(MESH_V1)}},
                           stored_refusal={"status": "error", "success": False, "http_status": 503,
                                           "code": "KILN_API_HTTP_ERROR", "error": "Down."})
        monkeypatch.setattr(courier, "_request", account)

        answer = courier.pull("cloud_pull_branch", "mug")

        assert answer["status"] == "success", answer
        assert account.note_gets() == []

    @HOW
    def test_no_recipe_this_computer_keeps_holds_a_note(self, home, monkeypatch, how):
        carve = _carve(MESH_V1)
        account = _Account({"v1": MESH_V1}, lists_notes=False, recipe_extra={
            "v1": {NOTES: {FACES: base64.b64encode(carve).decode("ascii")}},
        })
        monkeypatch.setattr(courier, "_request", account)

        assert _bring(how)["status"] == "success"

        vault = HistoryVault()
        try:
            assert NOTES not in json.dumps(vault.version("v1")["recipe"])
        finally:
            vault.close()
        for path in (home / ".kiln" / "designs" / "mug").glob(".kiln_recipe*.json"):
            assert NOTES not in path.read_text()
        assert _vault_note(_sha(MESH_V1), FACES) == carve
