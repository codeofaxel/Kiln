"""This computer's design-history records, and the courier that carries them.

On an install without kiln-pro the history tools run on Kiln's servers; the
history they work on is kept here (``kiln.history_vault``) and rides with
each call (``kiln.history_courier``).  These tests pin the records and the
courier, never a merge: there is no history logic on this side to test.

The answers in the hostile-answer cases are what a broken or impersonated
server could send.  None of them may write outside the design library or
read a file outside the vault's mesh folder.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from kiln import history_courier as courier
from kiln.history_vault import HistoryVault

MESH_V1 = b"solid one\nendsolid one\n"
MESH_V2 = b"solid two\nendsolid two\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KILN_DESIGNS_DIR", raising=False)
    return tmp_path


def _library(home: Path, design_id: str, versions: list[bytes]) -> Path:
    folder = home / ".kiln" / "designs" / design_id
    folder.mkdir(parents=True)
    for number, mesh in enumerate(versions, start=1):
        stl = folder / f"v{number}.stl"
        stl.write_bytes(mesh)
        recipe = {
            "name": design_id, "created": f"2026-10-0{number}T12:00:00+00:00", "version": number,
            "design_id": design_id, "source_scad": f"cube({number});", "stl_path": str(stl),
            "parameters": {"length": 50 + number}, "parts": [],
        }
        (folder / f".kiln_recipe.v{number}.json").write_text(json.dumps(recipe), encoding="utf-8")
        (folder / ".kiln_recipe.json").write_text(json.dumps(recipe), encoding="utf-8")
    return folder


def _vault(home: Path) -> HistoryVault:
    return HistoryVault()


class TestTheLibraryStartsTheHistory:
    def test_saved_versions_are_recorded_on_main_in_order(self, home):
        _library(home, "mug", [MESH_V1, MESH_V2])
        vault = _vault(home)
        assert vault.sync_from_library("mug") == 2
        main = vault.branch_named("mug", "main")
        members = [m["version_id"] for m in vault.memberships(main["branch_id"])]
        assert len(members) == 2
        assert main["head_version_id"] == members[-1]
        assert vault.version(members[1])["parent_version_id"] == members[0]

    def test_a_later_save_joins_the_end_of_main_once(self, home):
        folder = _library(home, "mug", [MESH_V1])
        vault = _vault(home)
        vault.sync_from_library("mug")
        (folder / "v2.stl").write_bytes(MESH_V2)
        (folder / ".kiln_recipe.v2.json").write_text(
            json.dumps({"name": "mug", "created": "2026-10-03T00:00:00+00:00", "version": 2,
                        "stl_path": str(folder / "v2.stl")}), encoding="utf-8"
        )
        assert vault.sync_from_library("mug") == 1
        assert vault.sync_from_library("mug") == 0
        main = vault.branch_named("mug", "main")
        assert len(vault.memberships(main["branch_id"])) == 2

    def test_a_design_id_naming_a_path_reads_nothing(self, home):
        _library(home, "mug", [MESH_V1])
        vault = _vault(home)
        (home / ".kiln" / "outside").mkdir()
        (home / ".kiln" / "outside" / ".kiln_recipe.v1.json").write_text(json.dumps({"version": 1}))
        assert vault.sync_from_library("../outside") == 0
        assert vault.sync_from_library("/etc") == 0


def _save(design_id: str, scad: str) -> dict:
    """A save through the public version tool, the way a pip install makes one."""
    from kiln.plugins import version_tools

    tools = {}

    class _MCP:
        def tool(self, *_a, **_k):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

    version_tools.plugin.register(_MCP())
    out = tools["save_design_version"](design_id=design_id, scad_source=scad)
    assert out.get("ok"), out
    return out["version"]


class TestSavesContinueTheCheckedOutBranch:
    def test_after_a_branch_is_made_the_next_save_continues_it(self, home):
        _library(home, "mug", [MESH_V1, MESH_V2])
        vault = _vault(home)
        vault.sync_from_library("mug")
        delta = _new_version_delta(vault, "mug")
        answer = courier.take_delta(
            {"status": "success", "branch": delta["branches"][0], "history_delta": {"designs": [delta]}},
            {"named": ["mug"]}, vault=vault, tool="create_design_branch",
        )
        assert answer["history_kept"]["saves_continue"] == "holes-in"

        _save("mug", "cube([1,2,3]);")
        vault.sync_from_library("mug")
        branch = vault.branch_named("mug", "holes-in")
        new_head = vault.version(branch["head_version_id"])
        assert new_head["parent_version_id"] == "f" * 32
        assert "cube([1,2,3])" in new_head["recipe"]["source_scad"]
        main = vault.branch_named("mug", "main")
        assert len(vault.memberships(main["branch_id"])) == 2

    def test_switching_back_to_main_never_writes_over_a_branchs_file(self, home):
        folder = _library(home, "mug", [MESH_V1])
        vault = _vault(home)
        vault.sync_from_library("mug")
        main = vault.branch_named("mug", "main")
        side = {**main, "branch_id": "c" * 32, "name": "side", "parent_branch_id": main["branch_id"],
                "base_version_id": main["head_version_id"]}
        vault._put_branch(side)
        vault._put_membership({"branch_id": side["branch_id"], "version_id": main["head_version_id"], "position": 0})
        vault._conn.commit()

        vault.check_out("mug", side["branch_id"])
        _save("mug", "sphere(2);")          # on side
        vault.sync_from_library("mug")
        vault.check_out("mug", main["branch_id"])
        _save("mug", "cylinder(h=4);")      # on main
        vault.sync_from_library("mug")

        files = " ".join(json.loads(p.read_text()).get("source_scad") or "" for p in folder.glob(".kiln_recipe.v*.json"))
        assert "sphere(2)" in files and "cylinder(h=4)" in files
        side_head = vault.version(vault.branch(side["branch_id"])["head_version_id"])
        main_head = vault.version(vault.branch(main["branch_id"])["head_version_id"])
        assert "sphere(2)" in side_head["recipe"]["source_scad"]
        assert "cylinder(h=4)" in main_head["recipe"]["source_scad"]

    def test_a_branch_that_is_not_this_designs_cannot_be_checked_out(self, home):
        _library(home, "mug", [MESH_V1])
        _library(home, "bowl", [MESH_V1])
        vault = _vault(home)
        vault.sync_from_library("mug")
        vault.sync_from_library("bowl")
        bowl_main = vault.branch_named("bowl", "main")
        assert vault.check_out("mug", bowl_main["branch_id"]) is None
        assert vault.checked_out("mug")["name"] == "main"


class TestTheLibraryFilesNameTheirVersion:
    def test_every_recorded_file_is_stamped_with_its_version(self, home):
        folder = _library(home, "mug", [MESH_V1, MESH_V2])
        vault = _vault(home)
        vault.sync_from_library("mug")
        stamped = {json.loads(p.read_text()).get("version_id") for p in folder.glob(".kiln_recipe.v*.json")}
        main = vault.branch_named("mug", "main")
        assert stamped == {m["version_id"] for m in vault.memberships(main["branch_id"])}

    def test_a_new_file_under_a_used_number_is_a_new_version(self, home):
        folder = _library(home, "mug", [MESH_V1])
        vault = _vault(home)
        vault.sync_from_library("mug")
        (folder / ".kiln_recipe.v1.json").write_text(json.dumps(
            {"name": "mug", "created": "2026-10-05T00:00:00+00:00", "version": 1, "source_scad": "other();"}))
        assert vault.sync_from_library("mug") == 1


class TestWhatACallSends:
    def test_the_bundle_carries_head_meshes_and_no_folder_of_this_computer(self, home):
        _library(home, "mug", [MESH_V1, MESH_V2])
        vault = _vault(home)
        vault.sync_from_library("mug")
        bundle = vault.bundle("mug")
        text = json.dumps(bundle)
        assert str(home) not in text
        head = vault.branch_named("mug", "main")["head_version_id"]
        meshes = {
            s["version_id"]: base64.b64decode(s["recipe_dict"]["mesh_bytes_b64"])
            for s in bundle["provenance_sidecars"] if "mesh_bytes_b64" in s["recipe_dict"]
        }
        assert meshes == {head: MESH_V2}

    def test_records_name_every_design_the_call_is_about(self, home):
        _library(home, "mug", [MESH_V1])
        vault = _vault(home)
        vault.sync_from_library("mug")
        main = vault.branch_named("mug", "main")
        sent = courier.records_for_call({"source_branch_id": main["branch_id"]}, vault=vault)
        assert [b["design_id"] for b in sent["designs"]] == ["mug"]
        assert sent["named"] == ["mug"]


def _new_version_delta(vault: HistoryVault, design_id: str, *, branch_name: str = "holes-in") -> dict:
    main = vault.branch_named(design_id, "main")
    base = main["head_version_id"]
    new_vid = "f" * 32
    branch = {
        "branch_id": "b" * 32, "design_id": design_id, "name": branch_name,
        "parent_branch_id": main["branch_id"], "base_version_id": base,
        "head_version_id": new_vid, "created_at": 1.0, "status": "active",
        "annotation": "", "metadata": {},
    }
    return {
        "design_id": design_id,
        "branches": [branch],
        "expected_heads": {},
        "versions": [{"version_id": new_vid, "parent_version_id": base, "created_at": 2.0}],
        "provenance_sidecars": [{
            "version_id": new_vid,
            "recipe_dict": {
                "version": 3, "stl_path": "part.stl",
                "mesh_bytes_b64": base64.b64encode(b"solid three\nendsolid three\n").decode(),
            },
        }],
        "memberships": [
            {"branch_id": branch["branch_id"], "version_id": base, "position": 0, "added_at": 1.0},
            {"branch_id": branch["branch_id"], "version_id": new_vid, "position": 1, "added_at": 2.0},
        ],
        "releases": [],
    }


class TestWhatComesBack:
    def test_a_new_branch_version_is_kept_and_main_stays_the_library_head(self, home):
        folder = _library(home, "mug", [MESH_V1, MESH_V2])
        vault = _vault(home)
        vault.sync_from_library("mug")
        delta = _new_version_delta(vault, "mug")
        answer = courier.take_delta({"status": "success", "history_delta": {"designs": [delta]}},
                                    {"named": ["mug"], "designs": []}, vault=vault)
        assert answer["history_kept"]["where"] == "on this computer"
        assert "history_delta" not in answer
        branch = vault.branch_named("mug", "holes-in")
        assert branch["head_version_id"] == "f" * 32
        assert vault.mesh_path(vault.version("f" * 32)["mesh_sha256"]).read_bytes().startswith(b"solid three")
        # The version is in the library, but the library's head is still main's,
        # numbered past every version so the next save takes a free number.
        assert any(json.loads(p.read_text()).get("version_id") == "f" * 32 for p in folder.glob(".kiln_recipe.*.json"))
        head = json.loads((folder / ".kiln_recipe.json").read_text())
        assert head["version_id"] == vault.branch_named("mug", "main")["head_version_id"]
        assert head["version"] == 3

    def test_a_branch_moved_here_meanwhile_is_left_as_it_is(self, home):
        _library(home, "mug", [MESH_V1])
        vault = _vault(home)
        vault.sync_from_library("mug")
        main = vault.branch_named("mug", "main")
        delta = {
            "design_id": "mug",
            "branches": [{**main, "head_version_id": "a" * 32}],
            "expected_heads": {main["branch_id"]: "not-the-head-any-more"},
            "versions": [], "memberships": [], "provenance_sidecars": [], "releases": [],
        }
        answer = courier.take_delta({"history_delta": {"designs": [delta]}}, {"named": ["mug"]}, vault=vault)
        assert vault.branch(main["branch_id"])["head_version_id"] == main["head_version_id"]
        assert answer["history_kept"]["not_moved"] == [main["branch_id"]]

    def test_an_answer_about_a_design_the_call_did_not_name_changes_nothing(self, home):
        _library(home, "mug", [MESH_V1])
        _library(home, "bowl", [MESH_V1])
        vault = _vault(home)
        vault.sync_from_library("mug")
        vault.sync_from_library("bowl")
        delta = _new_version_delta(vault, "bowl")
        answer = courier.take_delta({"history_delta": {"designs": [delta]}}, {"named": ["mug"]}, vault=vault)
        assert vault.branch_named("bowl", "holes-in") is None
        assert "history_kept" not in answer


class TestAHostileAnswer:
    def test_a_design_id_that_is_a_path_writes_no_file_outside_the_library(self, home):
        _library(home, "mug", [MESH_V1])
        vault = _vault(home)
        vault.sync_from_library("mug")
        delta = _new_version_delta(vault, "mug")
        delta["design_id"] = "../../escaped"
        courier.take_delta({"history_delta": {"designs": [delta]}}, {"named": ["../../escaped"]}, vault=vault)
        assert not (home / "escaped").exists()
        assert not (home / ".kiln" / "escaped").exists()

    def test_a_mesh_name_that_is_a_path_is_never_read(self, home):
        secret = home / "secret.stl"
        secret.write_bytes(b"not yours")
        _library(home, "mug", [MESH_V1])
        vault = _vault(home)
        vault.sync_from_library("mug")
        delta = _new_version_delta(vault, "mug")
        recipe = delta["provenance_sidecars"][0]["recipe_dict"]
        recipe.pop("mesh_bytes_b64")
        recipe["mesh_sha256"] = "../../../secret"
        courier.take_delta({"history_delta": {"designs": [delta]}}, {"named": ["mug"]}, vault=vault)
        assert vault.version("f" * 32)["mesh_sha256"] is None
        bundle = vault.bundle("mug", meshes_for={"f" * 32})
        assert b"not yours" not in json.dumps(bundle).encode()

    def test_a_version_id_with_a_slash_makes_a_plain_file_name(self, home):
        folder = _library(home, "mug", [MESH_V1])
        vault = _vault(home)
        vault.sync_from_library("mug")
        delta = _new_version_delta(vault, "mug")
        odd = "../../../../tmp/x"
        delta["versions"][0]["version_id"] = odd
        delta["provenance_sidecars"][0]["version_id"] = odd
        delta["provenance_sidecars"][0]["recipe_dict"].pop("version")
        delta["memberships"][1]["version_id"] = odd
        courier.take_delta({"history_delta": {"designs": [delta]}}, {"named": ["mug"]}, vault=vault)
        written = [p for p in folder.glob(".kiln_recipe.*.json") if json.loads(p.read_text()).get("version_id") == odd]
        assert len(written) == 1 and written[0].parent == folder

    def test_a_path_a_server_answer_names_never_reaches_the_library(self, home):
        secret = home / "secret.txt"
        secret.write_text("not yours")
        folder = _library(home, "mug", [MESH_V1])
        vault = _vault(home)
        vault.sync_from_library("mug")
        delta = _new_version_delta(vault, "mug")
        recipe = delta["provenance_sidecars"][0]["recipe_dict"]
        recipe.pop("mesh_bytes_b64")
        recipe["stl_path"] = str(secret)
        recipe["parts"] = [{"stl_path": str(secret)}]
        courier.take_delta({"history_delta": {"designs": [delta]}}, {"named": ["mug"]}, vault=vault)
        assert str(secret) not in " ".join(p.read_text() for p in folder.glob(".kiln_recipe*.json"))

    def test_a_library_recipe_naming_a_file_outside_its_folder_sends_nothing_of_it(self, home):
        secret = home / "secret.stl"
        secret.write_bytes(b"not yours")
        folder = home / ".kiln" / "designs" / "mug"
        folder.mkdir(parents=True)
        (folder / ".kiln_recipe.v1.json").write_text(json.dumps({
            "name": "mug", "created": "2026-10-01T00:00:00+00:00", "version": 1, "stl_path": str(secret),
        }))
        vault = _vault(home)
        vault.sync_from_library("mug")
        sent = json.dumps(vault.bundle("mug"))
        assert base64.b64encode(b"not yours").decode() not in sent
        assert vault.version(vault.branch_named("mug", "main")["head_version_id"])["mesh_sha256"] is None

    def test_an_answer_about_one_design_never_rewrites_anothers_records(self, home):
        _library(home, "mug", [MESH_V1])
        _library(home, "bowl", [MESH_V1])
        vault = _vault(home)
        vault.sync_from_library("mug")
        vault.sync_from_library("bowl")
        bowl_main = vault.branch_named("bowl", "main")
        bowl_head = bowl_main["head_version_id"]
        before = vault.version(bowl_head)
        vault._put_release({"release_id": "r-bowl", "design_id": "bowl", "name": "1.0.0"})
        vault._conn.commit()
        delta = {
            "design_id": "mug",
            "versions": [{"version_id": bowl_head, "parent_version_id": None, "created_at": 1.0}],
            "provenance_sidecars": [{"version_id": bowl_head, "recipe_dict": {"source_scad": "hijacked();"}}],
            "recipe_updates": [{"version_id": bowl_head, "recipe_dict": {"source_scad": "hijacked();"}}],
            "branches": [{**bowl_main, "design_id": "mug", "head_version_id": "x" * 32}],
            "memberships": [{"branch_id": bowl_main["branch_id"], "version_id": "x" * 32, "position": 9}],
            "releases": [{"release_id": "r-bowl", "name": "hijacked"}],
        }
        courier.take_delta({"history_delta": {"designs": [delta]}}, {"named": ["mug"]}, vault=vault)
        assert vault.version(bowl_head) == before
        assert vault.branch(bowl_main["branch_id"]) == bowl_main
        assert vault.design_of_release("r-bowl") == "bowl"
        assert [m["version_id"] for m in vault.memberships(bowl_main["branch_id"])] == [bowl_head]

    def test_rows_missing_their_ids_are_skipped_not_fatal(self, home):
        _library(home, "mug", [MESH_V1])
        vault = _vault(home)
        vault.sync_from_library("mug")
        delta = {
            "design_id": "mug",
            "branches": [{"name": "no-id"}, "not a row"],
            "versions": [{"parent_version_id": None}],
            "memberships": [{"branch_id": "x"}],
            "provenance_sidecars": [None],
            "releases": [{"signature": "s"}],
        }
        answer = courier.take_delta({"history_delta": {"designs": [delta]}}, {"named": ["mug"]}, vault=vault)
        assert answer["history_kept"]["designs"] == ["mug"]
        assert [b["name"] for b in vault.branches("mug")] == ["main"]


class TestPushCarriesTheBranch:
    def test_push_sends_each_version_with_its_mesh_and_no_local_folder(self, home, monkeypatch):
        _library(home, "mug", [MESH_V1, MESH_V2])
        sent = {}

        def fake_request(tool, method, path, body=None):
            sent.update(tool=tool, method=method, path=path, body=body)
            return {"pushed_versions": 2}

        monkeypatch.setattr(courier, "_request", fake_request)
        answer = courier.push("cloud_push_branch", "mug")
        assert answer["status"] == "success"
        assert sent["path"] == "/api/cloud/designs/mug/branches/push"
        body = sent["body"]
        assert body["branch"]["name"] == "main"
        assert [m["position"] for m in body["memberships"]] == [0, 1]
        meshes = [base64.b64decode(s["recipe_dict"]["mesh_bytes_b64"]) for s in body["provenance_sidecars"]]
        assert meshes == [MESH_V1, MESH_V2]
        assert str(home) not in json.dumps(body)

    def test_push_of_a_branch_this_computer_does_not_have_is_refused_here(self, home, monkeypatch):
        monkeypatch.setattr(courier, "_request", lambda *a, **k: pytest.fail("nothing should be sent"))
        answer = courier.push("cloud_push_branch", "mug", "nope")
        assert answer["code"] == "BRANCH_NOT_FOUND"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _account(stored: object, put_answer: dict | None = None):
    """A stand-in for Kiln's cloud routes; every call it receives is kept."""
    calls: list[tuple[str, str, object, bytes | None]] = []

    def answer(tool, method, path, body=None, *, content=None):
        calls.append((method, path, body, content))
        if path == "/api/cloud/meshes/stored":
            return stored if isinstance(stored, dict) else {"stored": list(stored)}
        if method == "PUT":
            return put_answer or {"sha256": path.rsplit("/", 1)[1], "stored": True}
        return {"pushed_versions": ["v1", "v2"]}

    return answer, calls


class TestAPushSendsEachModelOnce:
    def test_only_the_models_the_account_lacks_go_up_and_the_push_names_them(self, home, monkeypatch):
        _library(home, "mug", [MESH_V1, MESH_V2])
        answer, calls = _account([_sha(MESH_V1)])
        monkeypatch.setattr(courier, "_request", answer)
        assert courier.push("cloud_push_branch", "mug")["status"] == "success"
        assert calls[0][:3] == (
            "POST", "/api/cloud/meshes/stored", {"sha256s": sorted([_sha(MESH_V1), _sha(MESH_V2)])},
        )
        assert [(m, p, c) for m, p, _, c in calls if m == "PUT"] == [
            ("PUT", f"/api/cloud/meshes/{_sha(MESH_V2)}", MESH_V2),
        ]
        method, path, body, _ = calls[-1]
        assert (method, path) == ("POST", "/api/cloud/designs/mug/branches/push")
        assert [v["mesh_sha256"] for v in body["versions"]] == [_sha(MESH_V1), _sha(MESH_V2)]
        assert all("mesh_bytes_b64" not in s["recipe_dict"] for s in body["provenance_sidecars"])
        assert str(home) not in json.dumps(body)

    def test_a_server_that_does_not_know_the_question_gets_the_models_inside_the_push(self, home, monkeypatch):
        _library(home, "mug", [MESH_V1, MESH_V2])
        answer, calls = _account({
            "status": "error", "success": False, "code": "KILN_API_HTTP_ERROR", "http_status": 404,
        })
        monkeypatch.setattr(courier, "_request", answer)
        assert courier.push("cloud_push_branch", "mug")["status"] == "success"
        assert [m for m, *_ in calls] == ["POST", "POST"]
        body = calls[-1][2]
        assert all("mesh_sha256" not in v for v in body["versions"])
        meshes = [base64.b64decode(s["recipe_dict"]["mesh_bytes_b64"]) for s in body["provenance_sidecars"]]
        assert meshes == [MESH_V1, MESH_V2]

    def test_a_model_the_account_will_not_keep_stops_the_push_by_name(self, home, monkeypatch):
        _library(home, "mug", [MESH_V1, MESH_V2])
        answer, calls = _account([], put_answer={
            "status": "error", "success": False, "code": "MESH_TOO_LARGE", "http_status": 413,
            "error": "Kiln keeps models of up to 64 MB in your account.",
        })
        monkeypatch.setattr(courier, "_request", answer)
        out = courier.push("cloud_push_branch", "mug")
        assert out["status"] == "error" and out["code"] == "MESH_TOO_LARGE"
        assert out["version_id"] in out["error"]
        assert not [p for _, p, *_ in calls if p.endswith("/branches/push")]

    def test_a_refused_question_is_said_and_nothing_is_pushed(self, home, monkeypatch):
        _library(home, "mug", [MESH_V1])
        answer, calls = _account({
            "status": "error", "success": False, "code": "KILN_AUTH_REJECTED", "http_status": 401,
            "error": "Sign in again.",
        })
        monkeypatch.setattr(courier, "_request", answer)
        out = courier.push("cloud_push_branch", "mug")
        assert out["code"] == "KILN_AUTH_REJECTED"
        assert [p for _, p, *_ in calls] == ["/api/cloud/meshes/stored"]

    def test_a_model_goes_as_its_own_bytes(self, home, monkeypatch):
        import httpx

        monkeypatch.setenv("KILN_LICENSE_KEY", "kiln-test-key")
        sent = {}

        def fake(method, url, **kw):
            sent.update(method=method, url=url, **kw)
            return httpx.Response(200, json={"stored": True})

        monkeypatch.setattr(httpx, "request", fake)
        courier._request("cloud_push_branch", "PUT", "/api/cloud/meshes/" + _sha(MESH_V1), content=MESH_V1)
        assert sent["content"] == MESH_V1 and "json" not in sent
        assert sent["headers"]["Content-Type"] == "application/octet-stream"


def test_one_call_carries_at_most_one_mesh_budget_across_designs(home, monkeypatch):
    for design in ("mug", "bowl"):
        _library(home, design, [MESH_V1])
    monkeypatch.setattr(courier, "_MESH_BUDGET", len(MESH_V1))
    vault = _vault(home)
    sent = courier.records_for_call({"design_ids": ["mug", "bowl"]}, vault=vault)
    carried = [s for b in sent["designs"] for s in b["provenance_sidecars"] if "mesh_bytes_b64" in s["recipe_dict"]]
    assert len(carried) == 1
    assert any(b.get("meshes_left_out") for b in sent["designs"])


class TestARefusalIsAnError:
    def test_a_bare_not_found_from_the_server_is_an_error_not_an_empty_pull(self, home, monkeypatch):
        import httpx

        monkeypatch.setenv("KILN_LICENSE_KEY", "kiln-test-key")
        monkeypatch.setattr(
            httpx, "request",
            lambda *a, **k: httpx.Response(404, json={"error": "branch main not found"}),
        )
        answer = courier.pull("cloud_pull_branch", "mug")
        assert answer["status"] == "error" and answer["success"] is False
        assert answer["code"] == "NOT_FOUND" and answer["error"] == "branch main not found"
        vault = _vault(home)
        assert vault.branches("mug") == []


class TestAReleasePush:
    def test_a_release_kept_here_is_sent_whole_to_its_designs_route(self, home, monkeypatch):
        _library(home, "mug", [MESH_V1])
        vault = _vault(home)
        vault.sync_from_library("mug")
        record = {
            "release_id": "r1", "design_id": "mug", "version_id": "v1", "name": "1.0.0",
            "naming_scheme": "semver", "manifest": {"release_name": "1.0.0"},
            "signature_b64": "c2ln", "public_key_pem": "-----BEGIN PUBLIC KEY-----", "signed_at": 5.0,
            "signed_by": None, "status": "active",
        }
        vault._put_release(record)
        vault._conn.commit()
        vault.close()
        sent = {}
        monkeypatch.setattr(courier, "_request", lambda tool, method, path, body=None: sent.update(path=path, body=body) or {})
        answer = courier.run_local("cloud_push_release", "release_push", {"release_id": "r1"})
        assert answer["status"] == "success"
        assert sent["path"] == "/api/cloud/designs/mug/releases/push"
        assert sent["body"] == {k: record[k] for k in courier._RELEASE_PUSH_FIELDS}

    def test_a_release_this_computer_does_not_have_sends_nothing(self, home, monkeypatch):
        monkeypatch.setattr(courier, "_request", lambda *a, **k: pytest.fail("nothing should be sent"))
        assert courier.push_release("cloud_push_release", "nope")["code"] == "RELEASE_NOT_FOUND"


def test_the_couriers_are_the_cli_only_without_kiln_pro():
    import click

    from kiln.cli.history_commands import register_history_cli

    root = click.Group("kiln")
    register_history_cli(root)
    assert {"branch", "merge", "release", "cloud"} <= set(root.commands)
    assert "push" in root.commands["cloud"].commands["release"].commands
    taken = click.Group("kiln", commands={"release": click.Group("release")})
    register_history_cli(taken)
    assert taken.commands["release"].commands == {}  # an existing group is left as it is


class TestTheStubCarriesTheRecords:
    def test_a_served_history_stub_sends_records_and_keeps_what_comes_back(self, home, tmp_path, monkeypatch):
        from kiln import server

        _library(home, "mug", [MESH_V1])
        manifest = {"tools": [{
            "name": "create_design_branch", "description": "d", "tier": "pro",
            "history": {"schema_version": 1, "mode": "served"},
            "parameters": {"type": "object", "properties": {
                "design_id": {"type": "string"}, "name": {"type": "string"},
                "base_version_id": {"type": "string"}}, "required": ["design_id", "name"]},
        }]}
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "pro_tool_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        monkeypatch.setattr(server, "Path", lambda _p: pkg / "kiln")
        monkeypatch.setattr(server, "_PRO_TOOL_NUDGES", {})
        monkeypatch.setattr(server, "_PRO_TOOL_TIERS", {})
        monkeypatch.setattr(server, "_PRO_TOOL_QUOTA", {})
        seen = {}

        def fake_call(_tool, **kw):
            seen["records"] = kw.get("history_records")
            vault = HistoryVault()
            try:
                delta = _new_version_delta(vault, "mug")
            finally:
                vault.close()
            return {"status": "success", "branch": delta["branches"][0], "history_delta": {"designs": [delta]}}

        monkeypatch.setattr(server, "_pro_api_call", fake_call)
        registered = {}

        class _MCP:
            def tool(self, **_kwargs):
                def deco(fn):
                    registered[fn.__name__] = fn
                    return fn
                return deco

        server._register_pro_tool_stubs(_MCP())
        answer = registered["create_design_branch"](design_id="mug", name="holes-in")

        assert [b["design_id"] for b in seen["records"]["designs"]] == ["mug"]
        assert answer["history_kept"]["designs"] == ["mug"]
        vault = HistoryVault()
        try:
            assert vault.branch_named("mug", "holes-in") is not None
        finally:
            vault.close()


def test_the_mesh_folder_files_by_content(home):
    vault = _vault(home)
    sha = vault._put_mesh(MESH_V1)
    assert sha == hashlib.sha256(MESH_V1).hexdigest()
    assert vault.mesh_path(sha).read_bytes() == MESH_V1


# ---------------------------------------------------------------------------
# Saved features and decoration presets
# ---------------------------------------------------------------------------


def _feature_delta(item="f1", branch="fb1", head="fv2", parent="fv1", *, expected=None, art=None):
    version = {"feature_version_id": head, "feature_id": item, "name": "fillet",
               "parent_feature_version_id": parent, "version_number": 2}
    if art is not None:
        version["image_asset_bytes_b64"] = base64.b64encode(art).decode()
        version["image_asset_path"] = "/srv/ws/x/art.png"
    return {
        "feature_id": item,
        "branches": [{"branch_id": branch, "feature_id": item, "name": "main",
                      "head_feature_version_id": head}],
        "expected_heads": {branch: expected} if expected is not None or expected is None else {},
        "versions": [version],
        "memberships": [{"branch_id": branch, "feature_version_id": head, "position": 1}],
        "releases": [],
    }


class TestFeaturesAndPresetsAreKeptHere:
    def test_a_new_feature_from_the_server_is_kept_and_sent_back_whole(self, home):
        vault = _vault(home)
        answer = courier.take_delta(
            {"status": "success", "history_delta": {"features": [_feature_delta()]}},
            {"named_features": []}, vault=vault,
        )
        assert answer["history_kept"]["features"] == ["f1"]
        sent = courier.records_for_call({"feature_id": "f1"}, vault=vault, kinds=("features",))
        (bundle,) = sent["features"]
        assert [b["branch_id"] for b in bundle["branches"]] == ["fb1"]
        assert sent["named_features"] == ["f1"]

    def test_a_listing_sends_every_feature_kept_here(self, home):
        vault = _vault(home)
        for item in ("f1", "f2"):
            vault.apply_library_delta("features", _feature_delta(item=item, branch=f"b-{item}", head=f"v-{item}"))
        sent = courier.records_for_call({}, vault=vault, kinds=("features",), tool="list_features")
        assert sorted(b["feature_id"] for b in sent["features"]) == ["f1", "f2"]
        none = courier.records_for_call({}, vault=vault, kinds=("features",), tool="save_feature")
        assert none["features"] == []

    def test_a_branch_moved_here_meanwhile_is_left_as_it_is(self, home):
        vault = _vault(home)
        vault.apply_library_delta("features", _feature_delta())
        moved = vault.apply_library_delta("features", _feature_delta(head="fv3", parent="fv2", expected="fv-other"))
        assert moved["branches_changed_here"] == ["fb1"]
        (branch,) = vault.library_bundle("features", "f1")["branches"]
        assert branch["head_feature_version_id"] == "fv2"

    def test_an_answer_about_an_item_the_call_did_not_name_changes_nothing(self, home):
        vault = _vault(home)
        vault.apply_library_delta("features", _feature_delta(item="mine"))
        answer = courier.take_delta(
            {"history_delta": {"features": [_feature_delta(item="mine", head="evil", parent="fv2")]}},
            {"named_features": []}, vault=vault,
        )
        assert "history_kept" not in answer
        assert vault.library_bundle("features", "mine")["branches"][0]["head_feature_version_id"] == "fv2"

    def test_a_row_belonging_to_another_item_is_never_rewritten(self, home):
        vault = _vault(home)
        vault.apply_library_delta("features", _feature_delta(item="a", branch="shared-branch", head="va"))
        vault.apply_library_delta("features", _feature_delta(item="b", branch="shared-branch", head="vb"))
        assert vault.library_bundle("features", "a")["branches"][0]["head_feature_version_id"] == "va"
        assert vault.library_bundle("features", "b")["branches"] == []

    def test_art_is_kept_once_and_travels_back_by_name_never_by_the_servers_path(self, home):
        vault = _vault(home)
        art = b"\\x89PNG fake art"
        vault.apply_library_delta("presets", {
            "preset_id": "p1",
            "branches": [{"branch_id": "pb", "preset_id": "p1", "name": "main", "head_preset_version_id": "pv1"}],
            "versions": [{"preset_version_id": "pv1", "preset_id": "p1",
                          "image_asset_bytes_b64": base64.b64encode(art).decode(),
                          "image_asset_path": "/srv/ws/kiln/preset_assets/x.png"}],
            "memberships": [{"branch_id": "pb", "preset_version_id": "pv1", "position": 0}],
            "releases": [],
        })
        (kept,) = vault._lib_rows("presets", "version", "p1")
        assert "image_asset_bytes_b64" not in kept and kept["image_asset_sha256"] == hashlib.sha256(art).hexdigest()
        (sent,) = vault.library_bundle("presets", "p1")["versions"]
        assert base64.b64decode(sent["image_asset_bytes_b64"]) == art
        assert "/srv/" not in json.dumps(sent)


class TestALibraryPush:
    def test_a_feature_branch_is_pushed_from_this_computers_records(self, home, monkeypatch):
        vault = _vault(home)
        vault.apply_library_delta("features", _feature_delta())
        vault.close()
        sent = {}
        monkeypatch.setattr(courier, "_request", lambda tool, method, path, body=None: sent.update(path=path, body=body) or {})
        answer = courier.run_local("cloud_push_feature_branch", "feature_push", {"feature_id": "f1", "branch_id": "fb1"})
        assert answer["status"] == "success", answer
        assert sent["path"] == "/api/cloud/features/f1/branches/push"
        assert sent["body"]["branch"]["branch_id"] == "fb1"
        assert [v["feature_version_id"] for v in sent["body"]["versions"]] == ["fv2"]

    def test_a_pulled_preset_is_kept_here(self, home, monkeypatch):
        monkeypatch.setattr(courier, "_request", lambda *a, **k: {
            "branch": {"branch_id": "pb", "preset_id": "p9", "name": "main", "head_preset_version_id": "pv"},
            "versions": [{"preset_version_id": "pv", "preset_id": "p9"}],
            "memberships": [{"branch_id": "pb", "preset_version_id": "pv", "position": 0}],
        })
        answer = courier.run_local("cloud_pull_preset_branch", "preset_pull", {"preset_id": "p9"})
        assert answer["status"] == "success", answer
        vault = _vault(home)
        assert vault.has_library_item("presets", "p9")


def _log_entry(branch_id: str, old, new, *, design_id="mug", reflog_id=None, when=1.0, artifact_type="design"):
    return {
        "reflog_id": reflog_id or hashlib.sha256(f"{branch_id}{old}{new}{when}".encode()).hexdigest()[:32],
        "artifact_type": artifact_type, "artifact_id": design_id, "branch_id": branch_id,
        "branch_name": "main", "old_head": old, "new_head": new, "operation": "append_version",
        "operator": "me", "created_at": when, "metadata": {},
    }


class TestTheHistoryLogIsKeptHere:
    """The log (every head move Kiln's servers made for this computer) and
    each bisect's place come back in a served call's answer and go out with
    the calls that read them."""

    def test_entries_come_back_and_an_entry_kept_here_is_never_rewritten(self, home):
        vault = _vault(home)
        first = _log_entry("b1", None, "v1", when=1.0)
        courier.take_delta({"status": "success", "history_delta": {"designs": [], "reflog": [first]}}, {}, vault=vault)
        rewrite = {**first, "new_head": "planted"}
        answer = courier.take_delta(
            {"status": "success", "history_delta": {"designs": [], "reflog": [rewrite, _log_entry("b1", "v1", "v2", when=2.0)]}},
            {}, vault=vault,
        )
        assert answer["history_kept"]["log_entries"] == 1
        assert [e["new_head"] for e in vault.log_entries()] == ["v2", "v1"]

    def test_an_undo_sends_its_branchs_entries_and_the_design_it_moves(self, home):
        _library(home, "mug", [MESH_V1, MESH_V2])
        vault = _vault(home)
        vault.sync_from_library("mug")
        main = vault.branch_named("mug", "main")
        vault.apply_log_delta({"reflog": [
            _log_entry(main["branch_id"], "v1", main["head_version_id"], when=2.0),
            _log_entry("other-branch", None, "x", when=3.0, design_id="other"),
        ]})
        sent = courier.records_for_call(
            {"artifact_type": "design", "branch_id": main["branch_id"]},
            vault, kinds=("reflog",), tool="undo_last_head_change",
        )
        assert [e["branch_id"] for e in sent["reflog"]] == [main["branch_id"]]
        assert [b["design_id"] for b in sent["designs"]] == ["mug"] and sent["named"] == ["mug"]

    def test_a_restore_sends_the_one_entry_it_names(self, home):
        _library(home, "mug", [MESH_V1])
        vault = _vault(home)
        vault.sync_from_library("mug")
        entry = _log_entry(vault.branch_named("mug", "main")["branch_id"], None, "v1")
        vault.apply_log_delta({"reflog": [entry, _log_entry("b2", None, "w1", when=5.0)]})
        sent = courier.records_for_call(
            {"reflog_id": entry["reflog_id"]}, vault, kinds=("reflog",), tool="restore_from_reflog_entry",
        )
        assert [e["reflog_id"] for e in sent["reflog"]] == [entry["reflog_id"]]
        assert [b["design_id"] for b in sent["designs"]] == ["mug"]

    def test_a_search_keeps_its_place_and_sends_only_itself(self, home):
        vault = _vault(home)
        session = {"session_id": "s1", "artifact_type": "design", "artifact_id": "mug",
                   "chain": ["v1", "v2", "v3"], "good_set": ["v1"], "bad_set": ["v3"],
                   "skip_set": [], "candidate": "v2", "status": "active", "notes": {}}
        ended = {**session, "session_id": "s2", "status": "ended"}
        courier.take_delta({"status": "success", "history_delta": {"designs": [], "bisect": [session, ended]}}, {}, vault=vault)
        moved = {**session, "good_set": ["v1", "v2"], "candidate": None}
        courier.take_delta({"status": "success", "history_delta": {"designs": [], "bisect": [moved]}}, {}, vault=vault)
        one = courier.records_for_call({"session_id": "s1"}, vault, kinds=("bisect",), tool="bisect_good")
        assert one["bisect"] == [moved] and one["designs"] == []
        listed = courier.records_for_call({}, vault, kinds=("bisect",), tool="list_bisect_sessions")
        assert [s["session_id"] for s in listed["bisect"]] == ["s1"]

    def test_a_search_kept_for_one_artifact_is_never_rewritten_for_another(self, home):
        vault = _vault(home)
        session = {"session_id": "s1", "artifact_type": "design", "artifact_id": "mug",
                   "chain": ["v1", "v2"], "good_set": [], "bad_set": [], "skip_set": [],
                   "candidate": "v1", "status": "active", "notes": {}}
        vault.apply_log_delta({"bisect": [session]})
        kept = vault.apply_log_delta({"bisect": [{**session, "artifact_id": "bowl", "candidate": "v2"}]})
        assert kept["bisect_sessions"] == 0
        assert vault.bisect_session("s1") == session

    def test_rows_that_are_not_log_entries_are_skipped(self, home):
        vault = _vault(home)
        kept = vault.apply_log_delta({
            "reflog": [{"reflog_id": "r", "artifact_type": "printer", "artifact_id": "x", "branch_id": "b"}, "junk"],
            "bisect": [{"session_id": "", "artifact_type": "design", "artifact_id": "x"}],
        })
        assert kept == {"log_entries": 0, "bisect_sessions": 0}
        assert vault.log_entries() == [] and vault.bisect_sessions() == []


class TestTheRecipeFilesACallSends:
    """The provenance readers run on Kiln's servers on copies of this
    computer's own files; these are the files they get."""

    def test_a_pointer_to_any_other_file_sends_nothing_of_it(self, tmp_path):
        secret = tmp_path / "credentials.json"
        secret.write_text(json.dumps({"token": "not-for-the-servers"}))
        link = tmp_path / ".kiln_recipe.v0.json"
        link.symlink_to(secret)
        start = tmp_path / ".kiln_recipe.v2.json"
        start.write_text(json.dumps({"version_id": "v2", "parent_version": str(secret)}))
        sent = courier.files_for_call("get_design_ancestry", str(start))
        assert [i["path"] for i in sent["items"]] == [str(start)]
        start.write_text(json.dumps({"version_id": "v2", "parent_version": str(link)}))
        assert [i["path"] for i in courier.files_for_call("get_design_ancestry", str(start))["items"]] == [str(start)]
        assert courier.files_for_call("read_design_sidecar", str(secret))["items"] == []
        assert "not-for-the-servers" not in json.dumps(sent)

    def test_the_ancestry_sends_the_recipe_and_every_parent_it_names(self, tmp_path):
        a, b, c = (tmp_path / f".kiln_recipe.v{n}.json" for n in (1, 2, 3))
        a.write_text(json.dumps({"version_id": "v1"}))
        b.write_text(json.dumps({"version_id": "v2", "parent_version": str(a)}))
        c.write_text(json.dumps({"version_id": "v3", "parent_version": str(b)}))
        sent = courier.files_for_call("get_design_ancestry", str(c))
        assert [i["record"]["version_id"] for i in sent["items"]] == ["v3", "v2", "v1"]
        assert sent["arg"] == str(c)

    def test_a_cycle_or_a_missing_parent_ends_the_files(self, tmp_path):
        a, b = tmp_path / ".kiln_recipe.va.json", tmp_path / ".kiln_recipe.vb.json"
        a.write_text(json.dumps({"version_id": "a", "parent_version": str(b)}))
        b.write_text(json.dumps({"version_id": "b", "parent_version": str(a)}))
        assert len(courier.files_for_call("get_design_ancestry", str(a))["items"]) == 2
        b.write_text(json.dumps({"version_id": "b", "parent_version": str(tmp_path / ".kiln_recipe.gone.json")}))
        assert len(courier.files_for_call("get_design_ancestry", str(a))["items"]) == 2

    def test_the_sidecar_sends_what_sits_beside_the_model(self, tmp_path):
        stl = tmp_path / "part.stl"
        stl.write_bytes(b"solid\nendsolid\n")
        (tmp_path / ".kiln_recipe.json").write_text(json.dumps({"version_id": "v9"}))
        (tmp_path / "part.kiln.json").write_text(json.dumps({"legacy": True}))
        sent = courier.files_for_call("read_design_sidecar", str(stl))
        assert sorted(Path(i["path"]).name for i in sent["items"]) == [".kiln_recipe.json", "part.kiln.json"]
        assert all(i["record"] for i in sent["items"]) and sent["arg"] == str(stl)

    def test_the_answer_names_this_computers_own_paths_again(self, tmp_path):
        recipe = tmp_path / ".kiln_recipe.json"
        recipe.write_text(json.dumps({"version_id": "v1"}))
        sent = {"files": courier.files_for_call("get_design_ancestry", str(recipe))}
        answer = courier.take_delta(
            {"status": "success", "ancestry": [{"_recipe_path": "kiln-file:0"}], "note": "kiln-file:arg"}, sent,
        )
        assert answer["ancestry"][0]["_recipe_path"] == str(recipe) and answer["note"] == str(recipe)


class TestAFeaturesPartMeshIsKeptHere:
    def _delta(self, name: str, data: bytes) -> dict:
        d = _feature_delta()
        d["versions"][0].update({"part_asset_name": name, "part_asset_b64": base64.b64encode(data).decode()})
        return d

    def test_the_mesh_is_kept_by_its_digest_and_sent_back_with_its_version(self, home):
        vault = _vault(home)
        data = b"solid clip\nendsolid clip\n"
        name = f"{hashlib.sha256(data).hexdigest()}.stl"
        vault.apply_library_delta("features", self._delta(name, data))
        (version,) = [v for v in vault.library_bundle("features", "f1")["versions"] if v.get("part_asset_name")]
        assert base64.b64decode(version["part_asset_b64"]) == data

    def test_bytes_that_are_not_the_named_mesh_are_never_kept(self, home):
        vault = _vault(home)
        name = f"{hashlib.sha256(b'the real mesh').hexdigest()}.stl"
        vault.apply_library_delta("features", self._delta(name, b"something else"))
        assert all("part_asset_b64" not in v for v in vault.library_bundle("features", "f1")["versions"])


class TestArchiveAndDeleteOfAFeatureKeptHere:
    def test_an_archive_marks_each_version_and_an_unarchive_clears_it(self, home):
        vault = _vault(home)
        vault.apply_library_delta("features", _feature_delta())
        vids = [v["feature_version_id"] for v in vault.library_bundle("features", "f1")["versions"]]
        vault.apply_library_delta("features", {"feature_id": "f1", "archived_versions": {vids[0]: True, "not-mine": True}})
        flags = {v["feature_version_id"]: v.get("archived") for v in vault.library_bundle("features", "f1")["versions"]}
        assert flags[vids[0]] is True and "not-mine" not in flags
        vault.apply_library_delta("features", {"feature_id": "f1", "archived_versions": {vids[0]: False}})
        assert {v["feature_version_id"]: v.get("archived") for v in vault.library_bundle("features", "f1")["versions"]}[vids[0]] is False

    def test_a_permanent_delete_removes_the_item_and_nothing_else(self, home):
        vault = _vault(home)
        vault.apply_library_delta("features", _feature_delta())
        vault.apply_library_delta("features", _feature_delta(item="f2", branch="fb2", head="gv2", parent="gv1"))
        kept = vault.apply_library_delta("features", {"feature_id": "f1", "removed": True})
        assert kept["removed"] is True
        assert not vault.has_library_item("features", "f1") and vault.has_library_item("features", "f2")


class TestProvenRecipesKeptBesideTheirDesigns:
    def test_a_lookup_sends_every_proven_recipe_and_nothing_else(self, home):
        folder = _library(home, "mug", [MESH_V1])
        (folder / ".kiln_proven_recipe.json").write_text(json.dumps({"product_type": "mug"}))
        (folder / ".kiln_proven_recipe.v1.json").write_text(json.dumps({"product_type": "mug", "v": 1}))
        (folder / "notes.json").write_text(json.dumps({"secret": True}))
        sent = courier.records_for_call({}, _vault(home), kinds=("proven",), tool="list_proven_recipes")
        assert sorted(r["name"] for r in sent["proven"]) == [".kiln_proven_recipe.json", ".kiln_proven_recipe.v1.json"]
        assert all(r["design_id"] == "mug" for r in sent["proven"])

    def test_a_recorded_recipe_is_filed_beside_its_design_and_nowhere_else(self, home):
        folder = _library(home, "mug", [MESH_V1])
        bowl = _library(home, "bowl", [MESH_V1])
        (bowl / ".kiln_proven_recipe.json").write_text(json.dumps({"product_type": "bowl"}))
        answer = courier.take_delta({"status": "success", "history_delta": {"designs": [], "proven": [
            {"design_id": "mug", "name": ".kiln_proven_recipe.json", "record": {"product_type": "mug"}},
            {"design_id": "../outside", "name": ".kiln_proven_recipe.json", "record": {"x": 1}},
            {"design_id": "mug", "name": "../../evil.json", "record": {"x": 1}},
            # a design the call was not about: its recipe file is not this answer's to write
            {"design_id": "bowl", "name": ".kiln_proven_recipe.json", "record": {"product_type": "hijacked"}},
        ]}}, {"named": ["mug"]}, vault=_vault(home))
        assert answer["history_kept"]["proven_recipes"] == 1
        assert json.loads((folder / ".kiln_proven_recipe.json").read_text()) == {"product_type": "mug"}
        assert json.loads((bowl / ".kiln_proven_recipe.json").read_text()) == {"product_type": "bowl"}
        assert not (home / ".kiln" / "outside").exists() and not (home / "evil.json").exists()

    def test_a_listing_may_file_a_recipe_for_any_design_whose_recipes_it_sent(self, home):
        folder = _library(home, "mug", [MESH_V1])
        (folder / ".kiln_proven_recipe.json").write_text(json.dumps({"product_type": "mug"}))
        vault = _vault(home)
        sent = courier.records_for_call({}, vault, kinds=("proven",), tool="list_proven_recipes")
        answer = courier.take_delta({"status": "success", "history_delta": {"designs": [], "proven": [
            {"design_id": "mug", "name": ".kiln_proven_recipe.v1.json", "record": {"product_type": "mug", "v": 1}},
        ]}}, sent, vault=vault)
        assert answer["history_kept"]["proven_recipes"] == 1


# ---------------------------------------------------------------------------
# Proven recipes travel with their design
# ---------------------------------------------------------------------------

P1S = {"printer_model": "bambu_p1s", "material": "pla", "filament_line": None,
       "nozzle_diameter_mm": 0.4, "nozzle_material": None}


def _recipe(**over):
    return {"decoration_type": "logo_deboss", "product_type": "coaster", "parameters": {"deboss_depth": 0.6},
            "setup": P1S, "success": True, "notes": "crisp", "proven_at": "2026-10-01T00:00:00+00:00",
            "source_design": "mug", "recorded_by": "person", "confirmed": True, **over}


def _keep_recipes(folder: Path, name: str, recipes: list) -> None:
    (folder / name).write_text(json.dumps({"recipes": recipes}), encoding="utf-8")


def _held(folder: Path, name: str) -> list:
    return json.loads((folder / name).read_text(encoding="utf-8"))["recipes"]


class _Routes:
    """Kiln's cloud routes as a push or pull sees them: one answer per path."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, tool, method, path, body=None):
        self.calls.append((method, path, body))
        return self.answers.get(path.rsplit("/", 1)[-1], {})


class TestProvenRecipesTravelWithTheDesign:
    def test_a_push_sends_the_designs_recipes_naming_their_version(self, home, monkeypatch):
        folder = _library(home, "mug", [MESH_V1])
        _keep_recipes(folder, ".kiln_proven_recipe.v1.json",
                      [_recipe(parameters={"deboss_depth": 0.6, "logo_source": str(home / "logo.svg")})])
        other = _library(home, "bowl", [MESH_V1])
        _keep_recipes(other, ".kiln_proven_recipe.json", [_recipe(product_type="bowl")])
        routes = _Routes({"push": {"pushed_versions": 1, "kept": 1}})
        monkeypatch.setattr(courier, "_request", routes)

        answer = courier.push("cloud_push_branch", "mug")

        assert answer["status"] == "success", answer
        (method, path, body) = routes.calls[-1]
        assert (method, path) == ("POST", "/api/cloud/designs/mug/proven/push")
        (row,) = body["recipes"]
        assert row["version_ref"] == "1" and row["product_type"] == "coaster"
        assert row["setup"] == P1S and row["confirmed"] is True
        assert str(home) not in json.dumps(body)   # no file on this computer goes with it
        assert answer["proven_recipes"] == {"sent": 1, "kept": 1}

    def test_a_recipe_file_from_before_setups_is_sent_as_the_latests(self, home, monkeypatch):
        folder = _library(home, "mug", [MESH_V1])
        (folder / ".kiln_proven_recipe.json").write_text(
            json.dumps({"decoration_type": "logo_deboss", "product_type": "coaster", "parameters": {}}),
            encoding="utf-8")
        routes = _Routes({})
        monkeypatch.setattr(courier, "_request", routes)

        courier.push("cloud_push_branch", "mug")

        (row,) = routes.calls[-1][2]["recipes"]
        assert row["version_ref"] == "" and row["decoration_type"] == "logo_deboss"

    def test_a_design_with_no_recipes_sends_only_its_branch(self, home, monkeypatch):
        _library(home, "mug", [MESH_V1])
        routes = _Routes({})
        monkeypatch.setattr(courier, "_request", routes)

        answer = courier.push("cloud_push_branch", "mug")

        # The models' question, then the branch: no recipes call.
        assert [c[1] for c in routes.calls] == [
            "/api/cloud/meshes/stored", "/api/cloud/designs/mug/branches/push",
        ]
        assert "proven_recipes" not in answer

    def test_recipes_the_account_refused_leave_the_branch_pushed_and_say_so(self, home, monkeypatch):
        folder = _library(home, "mug", [MESH_V1])
        _keep_recipes(folder, ".kiln_proven_recipe.v1.json", [_recipe()])
        routes = _Routes({"push": {"pushed_versions": 1}})

        def answer_for(tool, method, path, body=None):
            if path.endswith("/proven/push"):
                return {"status": "error", "error": "try again in a moment", "code": "PROVEN_UNAVAILABLE"}
            return routes(tool, method, path, body)

        monkeypatch.setattr(courier, "_request", answer_for)

        answer = courier.push("cloud_push_branch", "mug")

        assert answer["status"] == "success"
        assert answer["proven_recipes"]["code"] == "PROVEN_UNAVAILABLE"
        assert answer["proven_recipes"]["sent"] == 0
        assert "not" in answer["proven_recipes"]["note"]

    def test_a_pull_keeps_the_accounts_recipes_beside_the_design(self, home, monkeypatch):
        folder = _library(home, "mug", [MESH_V1])
        routes = _Routes({"proven": {"recipes": [{"design_id": "mug", "version_ref": "1", **_recipe()}]}})
        monkeypatch.setattr(courier, "_request", routes)

        answer = courier.pull("cloud_pull_branch", "mug")

        assert answer["status"] == "success", answer
        assert ("GET", "/api/cloud/designs/mug/proven", None) in routes.calls
        (kept,) = _held(folder, ".kiln_proven_recipe.v1.json")
        assert kept["notes"] == "crisp" and kept["setup"] == P1S
        assert answer["proven_recipes"] == {"in_account": 1, "files_changed": 1}

    def test_a_clone_brings_them_too(self, home, monkeypatch):
        folder = _library(home, "mug", [MESH_V1])
        routes = _Routes({"proven": {"recipes": [{"design_id": "mug", "version_ref": "", **_recipe()}]}})
        monkeypatch.setattr(courier, "_request", routes)

        courier.clone("cloud_clone_design", "mug")

        assert [r["product_type"] for r in _held(folder, ".kiln_proven_recipe.json")] == ["coaster"]

    def test_an_automatic_recording_never_replaces_one_confirmed_here(self, home, monkeypatch):
        folder = _library(home, "mug", [MESH_V1])
        _keep_recipes(folder, ".kiln_proven_recipe.v1.json", [_recipe(notes="mine")])
        newer_auto = _recipe(notes="auto", recorded_by="auto", confirmed=False, proven_at="2026-10-09T00:00:00+00:00")
        monkeypatch.setattr(courier, "_request", _Routes({"proven": {"recipes": [
            {"design_id": "mug", "version_ref": "1", **newer_auto}]}}))

        courier.pull("cloud_pull_branch", "mug")

        (kept,) = _held(folder, ".kiln_proven_recipe.v1.json")
        assert kept["notes"] == "mine" and kept["confirmed"] is True

    def test_a_newer_confirmed_recipe_replaces_an_older_one_and_another_setup_joins(self, home, monkeypatch):
        folder = _library(home, "mug", [MESH_V1])
        _keep_recipes(folder, ".kiln_proven_recipe.v1.json", [_recipe(notes="old")])
        newer = _recipe(notes="new", proven_at="2026-10-09T00:00:00+00:00")
        petg = _recipe(notes="petg", setup={**P1S, "material": "petg"})
        monkeypatch.setattr(courier, "_request", _Routes({"proven": {"recipes": [
            {"design_id": "mug", "version_ref": "1", **newer}, {"design_id": "mug", "version_ref": "1", **petg}]}}))

        courier.pull("cloud_pull_branch", "mug")

        assert sorted(r["notes"] for r in _held(folder, ".kiln_proven_recipe.v1.json")) == ["new", "petg"]

    def test_a_version_that_names_a_path_writes_nothing(self, home, monkeypatch):
        folder = _library(home, "mug", [MESH_V1])
        before = sorted(p.name for p in folder.iterdir())
        monkeypatch.setattr(courier, "_request", _Routes({"proven": {"recipes": [
            {"design_id": "mug", "version_ref": "../../escape", **_recipe()},
            {"design_id": "bowl", "version_ref": "1", **_recipe()}]}}))

        answer = courier.pull("cloud_pull_branch", "mug")

        assert answer["proven_recipes"]["files_changed"] == 0
        assert sorted(p.name for p in folder.iterdir()) == before
        assert not (home / ".kiln" / "designs" / "bowl").exists()

    def test_recipes_the_account_could_not_send_leave_the_pull_done_and_say_so(self, home, monkeypatch):
        _library(home, "mug", [MESH_V1])

        def answer_for(tool, method, path, body=None):
            if path.endswith("/proven"):
                return {"status": "error", "error": "not found", "code": "NOT_FOUND"}
            return {}

        monkeypatch.setattr(courier, "_request", answer_for)

        answer = courier.pull("cloud_pull_branch", "mug")

        assert answer["status"] == "success"
        assert answer["proven_recipes"]["code"] == "NOT_FOUND"


class TestTheCommandSaysWhatBecameOfTheRecipes:
    def test_push_says_how_many_went_and_pull_says_when_they_did_not(self, home, monkeypatch):
        from click.testing import CliRunner

        import kiln.history_courier as hc
        from kiln.cli.history_commands import cloud_group

        monkeypatch.setattr(hc, "push", lambda *a, **k: {
            "status": "success", "proven_recipes": {"sent": 2, "kept": 2}})
        monkeypatch.setattr(hc, "pull", lambda *a, **k: {
            "status": "success", "proven_recipes": {
                "error": "busy \x1b[2Jnow", "note": "The design is here; its proven recipes are not."}})
        runner = CliRunner()

        pushed = runner.invoke(cloud_group, ["push", "mug"])
        pulled = runner.invoke(cloud_group, ["pull", "mug"])

        assert "Sent 2 proven recipes with it." in pushed.output, pushed.output
        assert "its proven recipes are not" in pulled.output and "\x1b" not in pulled.output


# ---------------------------------------------------------------------------
# A design locked to this computer
# ---------------------------------------------------------------------------

PREFS = "/api/cloud/user/prefs"


class _Account:
    """The person's Kiln account as this computer sees it: the automatic-saving
    switch, and the branch pushes.  Every call recorded; a push answers from
    *pushes* in order, then with an empty success."""

    def __init__(self, *, auto_backup=False, is_set=False, can=True, pushes=None, put=None):
        self.prefs = {"auto_backup": auto_backup, "is_set": is_set}
        if can is not None:
            self.prefs["can_auto_backup"] = can
        self.push_answers = list(pushes or [])
        self.put_answer = put
        self.calls = []

    def __call__(self, tool, method, path, body=None, **_kw):
        self.calls.append((method, path, body))
        if path == PREFS and method == "PUT":
            if self.put_answer is not None:
                return self.put_answer
            self.prefs.update(auto_backup=body["auto_backup"], is_set=True)
            return {"status": "success", "auto_backup": body["auto_backup"], "is_set": True}
        if path == PREFS:
            return {"status": "success", **self.prefs}
        if path.endswith("/branches/push"):
            return self.push_answers.pop(0) if self.push_answers else {"pushed_versions": []}
        return {}

    def pushes(self):
        return [body for method, path, body in self.calls if path.endswith("/branches/push")]

    def reads(self):
        return [c for c in self.calls if c[0] == "GET" and c[1] == PREFS]


def _diverged(tool="cloud_push_branch"):
    """A 409 from the push route, as ``_request`` hands it back."""
    return {
        "error": "branch diverged", "remote_head": "r" * 32, "local_parent": "p" * 32,
        "suggestion": "pull + rebase + push again", "status": "error", "success": False,
        "code": "KILN_API_HTTP_ERROR", "tool": tool, "http_status": 409,
    }


@pytest.fixture
def plain_install(monkeypatch):
    """No kiln-pro: any ``import kiln_pro...`` raises ImportError."""
    import sys

    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


def _lock(design_id: str, locked: bool = True) -> None:
    vault = HistoryVault()
    try:
        vault.set_locked(design_id, locked)
    finally:
        vault.close()


class TestALockedDesignStaysHere:
    def test_the_lock_is_kept_across_a_restart_and_read_from_anywhere(self, home):
        from kiln.history_vault import design_locked

        assert design_locked("mug") is False          # no records on this computer at all
        _lock("mug")
        assert design_locked("mug") is True and design_locked("bowl") is False
        _lock("mug", False)
        assert design_locked("mug") is False

    def test_a_push_of_a_locked_design_is_refused_and_sends_nothing(self, home, monkeypatch):
        _library(home, "mug", [MESH_V1])
        _lock("mug")
        monkeypatch.setattr(courier, "_request", lambda *a, **k: pytest.fail("nothing should be sent"))

        answer = courier.push("cloud_push_branch", "mug")

        assert answer["code"] == "DESIGN_LOCKED" and answer["success"] is False
        said = answer["error"]
        assert "mug is locked to this computer" in said and "kept on this computer" in said
        assert "kiln design unlock mug" in said
        assert answer["unlock"]["arguments"] == {"design_id": "mug", "locked": False}

    def test_another_spelling_of_a_locked_design_is_the_same_locked_design(self, home, monkeypatch):
        """The lock judged the spelling while a push resolves the folder, so
        ``./mug`` (and ``Mug`` on a disk that ignores case) reached the locked
        ``mug`` folder and was sent.  Found by a security review of the commit
        that added the lock, 2026-10-18; it fails at 23c84153."""
        from kiln.history_vault import design_locked

        _library(home, "mug", [MESH_V1])
        _lock("mug")
        monkeypatch.setattr(courier, "_request", lambda *a, **k: pytest.fail("nothing should be sent"))

        assert design_locked("./mug") is True and design_locked("mug/") is True
        assert design_locked("MUG") is True       # folded: two designs may share a lock, a locked one never slips
        answer = courier.push("cloud_push_branch", "./mug")
        assert answer["success"] is False and answer["code"] in ("INVALID_INPUT", "DESIGN_LOCKED")

    def test_the_mcp_push_door_is_refused_too(self, home, monkeypatch):
        _library(home, "mug", [MESH_V1])
        _lock("mug")
        monkeypatch.setattr(courier, "_request", lambda *a, **k: pytest.fail("nothing should be sent"))
        answer = courier.run_local("cloud_push_branch", "push", {"design_id": "mug"})
        assert answer["code"] == "DESIGN_LOCKED"

    def test_a_locked_designs_release_is_not_pushed(self, home, monkeypatch):
        _library(home, "mug", [MESH_V1])
        vault = _vault(home)
        vault._put_release({"release_id": "r1", "design_id": "mug", "name": "1.0.0"})
        vault._conn.commit()
        vault.set_locked("mug", True)
        vault.close()
        monkeypatch.setattr(courier, "_request", lambda *a, **k: pytest.fail("nothing should be sent"))

        answer = courier.run_local("cloud_push_release", "release_push", {"release_id": "r1"})

        assert answer["code"] == "DESIGN_LOCKED"
        assert "did not save its release to your Kiln account" in answer["error"]

    # A lap found (2026-10-20; fails at 9744b7e8): a feature or preset push
    # sent the design it was made for as a label, so a design locked to this
    # computer still had its name reach the account.  The feature's own files
    # are its own; only the name of the locked design stays here.
    def test_a_feature_pushed_for_a_locked_design_leaves_the_designs_name_here(self, home, monkeypatch):
        vault = _vault(home)
        vault._lib_put("features", "branch", "fb1", "f1", {"branch_id": "fb1", "feature_id": "f1", "name": "main"})
        vault._lib_put("features", "version", "fv1", "f1",
                       {"feature_version_id": "fv1", "feature_id": "f1", "design_id": "mug"})
        vault._lib_put("features", "membership", "fb1:fv1", "f1",
                       {"branch_id": "fb1", "feature_version_id": "fv1", "position": 0})
        vault._conn.commit()
        vault.set_locked("mug", True)
        vault.close()
        sent = []
        monkeypatch.setattr(courier, "_request", lambda tool, method, path, body=None: sent.append(body) or {})

        locked = courier.push_library("cloud_push_feature_branch", "features", "f1", "fb1", design_id="mug")
        open_ = courier.push_library("cloud_push_feature_branch", "features", "f1", "fb1", design_id="bowl")

        assert locked["status"] == "success"
        assert sent[0]["design_id"] is None and sent[0]["versions"]
        assert "mug" not in str(sent[0])          # nor on the feature's own records
        assert "mug is locked to this computer" in locked["design_left_out"]
        assert sent[1]["design_id"] == "bowl" and "mug" not in str(sent[1])
        assert "mug is locked" in open_["design_left_out"]   # its own record named mug

    def test_the_history_log_push_leaves_a_locked_designs_entries_here(self, home):
        vault = _vault(home)
        vault.apply_log_delta({"reflog": [
            _log_entry("b-mug", None, "v1", design_id="mug", when=1.0),
            _log_entry("b-bowl", None, "w1", design_id="bowl", when=2.0),
        ]})
        vault.set_locked("mug", True)
        sent = courier.records_for_call({}, vault, kinds=("reflog",), tool="push_reflog_to_cloud")
        assert [e["artifact_id"] for e in sent["reflog"]] == ["bowl"]
        shown = courier.records_for_call({}, vault, kinds=("reflog",), tool="show_reflog")
        assert sorted(e["artifact_id"] for e in shown["reflog"]) == ["bowl", "mug"]


def _register_stubs(monkeypatch, tmp_path, tools: list[dict], fake_call) -> dict:
    """The paid-tool stubs for *tools*, as a plain install registers them."""
    from kiln import server

    pkg = tmp_path / "pkg"
    pkg.mkdir(exist_ok=True)
    (pkg / "pro_tool_manifest.json").write_text(json.dumps({"tools": tools}), encoding="utf-8")
    monkeypatch.setattr(server, "Path", lambda _p: pkg / "kiln")
    monkeypatch.setattr(server, "_PRO_TOOL_NUDGES", {})
    monkeypatch.setattr(server, "_PRO_TOOL_TIERS", {})
    monkeypatch.setattr(server, "_PRO_TOOL_QUOTA", {})
    monkeypatch.setattr(server, "_pro_api_call", fake_call)
    registered = {}

    class _MCP:
        def tool(self, **_kwargs):
            def deco(fn):
                registered[fn.__name__] = fn
                return fn
            return deco

    server._register_pro_tool_stubs(_MCP())
    return registered


def _served_tool(name: str, params: dict) -> dict:
    return {
        "name": name, "description": "d", "tier": "pro",
        "history": {"schema_version": 1, "mode": "served"},
        "parameters": {"type": "object", "properties": {k: {"type": "string"} for k in params},
                       "required": list(params)},
    }


class TestServedCallsOfALockedDesign:
    def test_a_pull_request_is_refused_before_anything_is_sent(self, home, tmp_path, monkeypatch):
        _library(home, "mug", [MESH_V1])
        _lock("mug")
        calls = []
        stubs = _register_stubs(
            monkeypatch, tmp_path,
            [_served_tool("propose_design_change", {"design_id": 1, "source_branch_id": 1, "target_branch_id": 1})],
            lambda tool, **kw: calls.append(tool) or {"status": "success"},
        )

        answer = stubs["propose_design_change"](design_id="mug", source_branch_id="x", target_branch_id="y")

        assert answer["code"] == "DESIGN_LOCKED" and calls == []
        assert "did not open a pull request for it in your Kiln account" in answer["error"]

    def test_a_branch_of_a_locked_design_is_still_made_and_kept_here(self, home, tmp_path, monkeypatch):
        _library(home, "mug", [MESH_V1])
        _lock("mug")

        def fake_call(_tool, **kw):
            vault = HistoryVault()
            try:
                delta = _new_version_delta(vault, "mug")
            finally:
                vault.close()
            return {"status": "success", "branch": delta["branches"][0], "history_delta": {"designs": [delta]}}

        stubs = _register_stubs(
            monkeypatch, tmp_path, [_served_tool("create_design_branch", {"design_id": 1, "name": 1})], fake_call,
        )
        answer = stubs["create_design_branch"](design_id="mug", name="holes-in")

        assert answer["history_kept"]["designs"] == ["mug"]
        vault = HistoryVault()
        try:
            assert vault.branch_named("mug", "holes-in") is not None
        finally:
            vault.close()


def _version_tools() -> dict:
    from kiln.plugins import version_tools

    tools = {}

    class _MCP:
        def tool(self, *_a, **_k):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

    version_tools.plugin.register(_MCP())
    return tools


class TestTheLockTool:
    def test_lock_and_unlock_say_what_they_mean(self, home):
        from kiln.history_vault import design_locked

        lock_design = _version_tools()["lock_design"]
        locked = lock_design(design_id="mug")
        assert locked["ok"] is True and locked["locked"] is True and design_locked("mug")
        assert "will not save it to your Kiln account" in locked["said"]
        assert "passes through them for that call only and is not kept there" in locked["said"]

        unlocked = lock_design(design_id="mug", locked=False)
        assert unlocked["locked"] is False and not design_locked("mug")

    def test_a_design_id_that_is_a_path_locks_nothing(self, home):
        from kiln.history_vault import design_locked

        answer = _version_tools()["lock_design"](design_id="../outside")
        assert answer["ok"] is False and not design_locked("../outside")


class TestTheLockCommands:
    def test_design_lock_and_unlock_on_a_cli_without_a_design_group(self, home):
        import click
        from click.testing import CliRunner

        from kiln.cli.history_commands import register_design_lock_cli
        from kiln.history_vault import design_locked

        root = click.Group("kiln")
        register_design_lock_cli(root)
        runner = CliRunner()
        out = runner.invoke(root, ["design", "lock", "mug"])
        assert out.exit_code == 0, out.output
        assert "mug is locked to this computer" in out.output and design_locked("mug")
        out = runner.invoke(root, ["design", "unlock", "mug"])
        assert out.exit_code == 0 and "mug is unlocked" in out.output and not design_locked("mug")

    def test_the_commands_join_a_design_group_the_cli_already_has(self):
        import click

        from kiln.cli.history_commands import register_design_lock_cli

        existing = click.Group("design", commands={"archive": click.Command("archive")})
        root = click.Group("kiln", commands={"design": existing})
        register_design_lock_cli(root)
        assert root.commands["design"] is existing
        assert {"archive", "lock", "unlock"} <= set(existing.commands)


# ---------------------------------------------------------------------------
# Automatic saving to the account (Kiln Pro, off until turned on)
# ---------------------------------------------------------------------------


class TestASaveIsPushedWhenAutoPushIsOn:
    def test_the_saved_version_goes_up_on_its_branch(self, home, monkeypatch, plain_install):
        account = _Account(auto_backup=True, is_set=True)
        monkeypatch.setattr(courier, "_request", account)

        out = _save_answer("mug", "cube(1);")

        assert out["ok"] is True
        assert out["auto_push"]["pushed"] is True, out["auto_push"]
        (body,) = account.pushes()
        assert body["branch"]["name"] == "main"
        assert "cube(1)" in body["provenance_sidecars"][-1]["recipe_dict"]["source_scad"]
        assert "Saved main of mug to your Kiln account." in out["auto_push"]["said"]

    def test_nothing_goes_up_while_it_is_off(self, home, monkeypatch, plain_install):
        account = _Account(auto_backup=False, is_set=True)
        monkeypatch.setattr(courier, "_request", account)

        out = _save_answer("mug", "cube(1);")

        assert out["ok"] is True and "auto_push" not in out
        assert account.pushes() == []

    def test_an_account_that_does_not_say_it_may_is_not_pushed_for(self, home, monkeypatch, plain_install):
        account = _Account(auto_backup=True, is_set=True, can=None)
        monkeypatch.setattr(courier, "_request", account)

        out = _save_answer("mug", "cube(1);")

        assert "auto_push" not in out and account.pushes() == []

    def test_a_refused_push_never_fails_the_save_and_says_so(self, home, monkeypatch, plain_install):
        account = _Account(auto_backup=True, is_set=True, pushes=[
            {"status": "error", "success": False, "code": "STORE_UNAVAILABLE", "error": "try again shortly"}])
        monkeypatch.setattr(courier, "_request", account)

        out = _save_answer("mug", "cube(1);")

        assert out["ok"] is True and out["version"]["version"] == 1
        auto = out["auto_push"]
        assert auto["pushed"] is False and auto["branches"][0]["code"] == "STORE_UNAVAILABLE"
        assert "kept on this computer" in auto["said"] and "kiln cloud push mug" in auto["said"]

    def test_a_locked_design_is_not_pushed_automatically(self, home, monkeypatch, plain_install):
        _lock("mug")
        account = _Account(auto_backup=True, is_set=True)
        monkeypatch.setattr(courier, "_request", account)

        out = _save_answer("mug", "cube(1);")

        assert account.pushes() == []
        assert out["auto_push"]["branches"][0]["code"] == "DESIGN_LOCKED"
        assert "mug is locked to this computer" in out["auto_push"]["said"]

    def test_the_switch_is_read_at_most_once_a_minute(self, home, monkeypatch, plain_install):
        account = _Account(auto_backup=True, is_set=True)
        monkeypatch.setattr(courier, "_request", account)

        _save_answer("mug", "cube(1);")
        _save_answer("mug", "cube(2);")
        assert len(account.reads()) == 1 and len(account.pushes()) == 2

        vault = HistoryVault()
        try:
            kept = vault.setting("account_prefs")
            vault.set_setting("account_prefs", {**kept, "read_at": kept["read_at"] - 61})
        finally:
            vault.close()
        _save_answer("mug", "cube(3);")
        assert len(account.reads()) == 2

    def test_a_switch_that_cannot_be_read_pushes_nothing_and_says_so(self, home, monkeypatch, plain_install):
        account = _Account(auto_backup=True, is_set=True)
        monkeypatch.setattr(courier, "_request", account)
        _save_answer("mug", "cube(1);")
        vault = HistoryVault()
        try:
            kept = vault.setting("account_prefs")
            vault.set_setting("account_prefs", {**kept, "read_at": kept["read_at"] - 61})
        finally:
            vault.close()
        offline = {"status": "error", "success": False, "code": "SERVER_UNREACHABLE", "error": "no answer"}
        monkeypatch.setattr(courier, "_request", lambda *a, **k: offline)

        out = _save_answer("mug", "cube(2);")

        assert out["ok"] is True
        assert out["auto_push"]["pushed"] is False and out["auto_push"]["code"] == "SERVER_UNREACHABLE"

    def test_a_branch_a_served_call_moved_is_pushed(self, home, monkeypatch):
        _library(home, "mug", [MESH_V1, MESH_V2])
        vault = _vault(home)
        vault.sync_from_library("mug")
        account = _Account(auto_backup=True, is_set=True)
        monkeypatch.setattr(courier, "_request", account)
        delta = _new_version_delta(vault, "mug")

        answer = courier.take_delta(
            {"status": "success", "branch": delta["branches"][0], "history_delta": {"designs": [delta]}},
            {"named": ["mug"]}, vault=vault, tool="create_design_branch",
        )

        (body,) = account.pushes()
        assert body["branch"]["branch_id"] == "b" * 32 and body["branch"]["name"] == "holes-in"
        assert answer["auto_push"]["pushed"] is True
        assert answer["history_kept"]["saves_continue"] == "holes-in"

    def test_a_served_call_that_moved_nothing_pushes_nothing(self, home, monkeypatch):
        _library(home, "mug", [MESH_V1])
        vault = _vault(home)
        vault.sync_from_library("mug")
        account = _Account(auto_backup=True, is_set=True)
        monkeypatch.setattr(courier, "_request", account)
        main = vault.branch_named("mug", "main")
        delta = {"design_id": "mug", "branches": [main], "versions": [], "memberships": [],
                 "provenance_sidecars": [], "releases": []}

        answer = courier.take_delta({"history_delta": {"designs": [delta]}}, {"named": ["mug"]}, vault=vault)

        assert account.calls == [] and "auto_push" not in answer


    def test_a_rollback_is_a_save_and_goes_up_too(self, home, monkeypatch, plain_install):
        monkeypatch.setattr(courier, "_request", _Account(auto_backup=False, is_set=True))
        _save_answer("mug", "cube(1);")
        _save_answer("mug", "cube(2);")
        account = _Account(auto_backup=True, is_set=True)
        monkeypatch.setattr(courier, "_request", account)
        vault = HistoryVault()
        try:
            vault.set_setting("account_prefs", None)
        finally:
            vault.close()

        out = _version_tools()["rollback_design_version"](design_id="mug", to_version_id="1")

        assert out["ok"] is True and out["auto_push"]["pushed"] is True
        (body,) = account.pushes()
        assert "cube(1)" in body["provenance_sidecars"][-1]["recipe_dict"]["source_scad"]

    def test_with_kiln_pro_installed_the_save_leaves_it_to_kiln_pro(self, home, monkeypatch):
        """kiln-pro records and pushes its own versions: an ImportError inside
        its own path never sends anything from here."""
        import sys
        import types

        for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
            monkeypatch.delitem(sys.modules, name)
        monkeypatch.setitem(sys.modules, "kiln_pro", types.ModuleType("kiln_pro"))
        account = _Account(auto_backup=True, is_set=True)
        monkeypatch.setattr(courier, "_request", account)

        out = _save_answer("mug", "cube(1);")

        assert out["ok"] is True and "auto_push" not in out and account.calls == []

def _save_answer(design_id: str, scad: str) -> dict:
    from kiln.plugins import version_tools

    tools = {}

    class _MCP:
        def tool(self, *_a, **_k):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

    version_tools.plugin.register(_MCP())
    return tools["save_design_version"](design_id=design_id, scad_source=scad)


class TestTheOneTimeOffer:
    def test_made_once_when_a_design_reaches_its_third_version(self, home, monkeypatch, plain_install):
        account = _Account(auto_backup=False, is_set=False)
        monkeypatch.setattr(courier, "_request", account)

        firsts = [_save_answer("mug", f"cube({n});") for n in (1, 2)]
        third = _save_answer("mug", "cube(3);")
        fourth = _save_answer("mug", "cube(4);")

        assert all("auto_push_offer" not in a for a in firsts)
        offer = third["auto_push_offer"]
        assert offer["turn_on"] == "kiln cloud auto-push on"
        assert "stays off until you turn it on" in offer["said"]
        assert "auto_push_offer" not in fourth and "auto_push" not in third
        assert account.pushes() == []

    def test_made_when_a_branch_is_first_made_here(self, home, monkeypatch):
        _library(home, "mug", [MESH_V1])
        vault = _vault(home)
        vault.sync_from_library("mug")
        monkeypatch.setattr(courier, "_request", _Account(auto_backup=False, is_set=False))
        delta = _new_version_delta(vault, "mug")

        answer = courier.take_delta(
            {"status": "success", "branch": delta["branches"][0], "history_delta": {"designs": [delta]}},
            {"named": ["mug"]}, vault=vault, tool="create_design_branch",
        )

        assert answer["auto_push_offer"]["turn_on"] == "kiln cloud auto-push on"
        assert vault.setting("auto_push_offered")

    def test_never_made_when_the_plan_does_not_include_it_or_the_switch_was_set(self, home, monkeypatch, plain_install):
        monkeypatch.setattr(courier, "_request", _Account(auto_backup=False, is_set=False, can=False))
        free = [_save_answer("mug", f"cube({n});") for n in (1, 2, 3)]
        assert all("auto_push_offer" not in a for a in free)

        monkeypatch.setattr(courier, "_request", _Account(auto_backup=False, is_set=True, can=True))
        vault = HistoryVault()
        try:
            vault.set_setting("account_prefs", None)
        finally:
            vault.close()
        assert "auto_push_offer" not in _save_answer("mug", "cube(4);")


class TestTheAutoPushCommand:
    def test_on_sets_the_accounts_switch_and_says_what_it_does(self, home, monkeypatch):
        from click.testing import CliRunner

        from kiln.cli.history_commands import cloud_group

        account = _Account(auto_backup=False, is_set=False)
        monkeypatch.setattr(courier, "_request", account)

        out = CliRunner().invoke(cloud_group, ["auto-push", "on"])

        assert out.exit_code == 0, out.output
        assert ("PUT", PREFS, {"auto_backup": True}) in account.calls
        assert "Automatic saving is on" in out.output
        status = CliRunner().invoke(cloud_group, ["auto-push", "status"])
        assert "Automatic saving is on" in status.output

    def test_a_plan_without_it_hears_the_servers_own_words(self, home, monkeypatch):
        from click.testing import CliRunner

        from kiln.cli.history_commands import cloud_group

        monkeypatch.setattr(courier, "_request", _Account(put={
            "status": "error", "success": False, "code": "PRO_REQUIRED",
            "error": "Automatic saving is part of Kiln Pro.", "http_status": 402}))

        out = CliRunner().invoke(cloud_group, ["auto-push", "on"])

        assert out.exit_code == 1 and "Automatic saving is part of Kiln Pro." in out.output


# ---------------------------------------------------------------------------
# Two computers, one branch
# ---------------------------------------------------------------------------


class TestABranchThatMovedOnAnotherComputer:
    def test_this_computers_work_is_kept_as_its_own_branch_and_nothing_is_merged(self, home, monkeypatch):
        import socket

        monkeypatch.setattr(socket, "gethostname", lambda: "Adams-MacBook.local")
        _library(home, "mug", [MESH_V1, MESH_V2])
        account = _Account(pushes=[_diverged()])
        monkeypatch.setattr(courier, "_request", account)

        answer = courier.push("cloud_push_branch", "mug")

        assert answer["status"] == "success", answer
        first, second = account.pushes()
        main = first["branch"]
        own = second["branch"]
        assert own["name"] == "main@adams-macbook"
        assert own["parent_branch_id"] == main["branch_id"] and own["branch_id"] != main["branch_id"]
        assert {m["branch_id"] for m in second["memberships"]} == {own["branch_id"]}
        assert [m["version_id"] for m in second["memberships"]] == [m["version_id"] for m in first["memberships"]]
        assert answer["kept_as"] == "main@adams-macbook"
        said = answer["said"]
        assert "moved on another computer" in said and "main@adams-macbook" in said and "Nothing was merged" in said
        assert "kiln merge check mug main@adams-macbook main" in said
        assert "kiln merge run mug main@adams-macbook main" in said
        assert not any("merge" in path for _m, path, _b in account.calls)
        vault = _vault(home)
        kept = vault.branch_named("mug", "main@adams-macbook")
        assert kept["head_version_id"] == main["head_version_id"]
        assert vault.branch(main["branch_id"])["head_version_id"] == main["head_version_id"]

    def test_the_branch_is_the_same_one_every_push_and_the_name_is_kept(self, home, monkeypatch):
        import socket

        monkeypatch.setattr(socket, "gethostname", lambda: "Shop-PC")
        _library(home, "mug", [MESH_V1])
        account = _Account(pushes=[_diverged(), {}, _diverged(), {}])
        monkeypatch.setattr(courier, "_request", account)

        courier.push("cloud_push_branch", "mug")
        monkeypatch.setattr(socket, "gethostname", lambda: "renamed-later")
        courier.push("cloud_push_branch", "mug")

        ids = [b["branch"]["branch_id"] for b in account.pushes()]
        names = [b["branch"]["name"] for b in account.pushes()]
        assert ids[1] == ids[3] and names[1] == names[3] == "main@shop-pc"

    def test_a_free_caller_keeps_the_servers_own_answer(self, home, monkeypatch):
        _library(home, "mug", [MESH_V1])
        plan = {"status": "error", "success": False, "code": "PRO_REQUIRED", "http_status": 402,
                "error": "Branching is a Kiln Pro feature.", "required_tier": "pro"}
        account = _Account(pushes=[_diverged(), plan])
        monkeypatch.setattr(courier, "_request", account)

        answer = courier.push("cloud_push_branch", "mug")

        assert answer == _diverged()
        vault = _vault(home)
        assert [b["name"] for b in vault.branches("mug")] == ["main"]

    def test_an_automatic_push_keeps_its_own_branch_too(self, home, monkeypatch, plain_install):
        import socket

        monkeypatch.setattr(socket, "gethostname", lambda: "laptop")
        account = _Account(auto_backup=True, is_set=True, pushes=[_diverged()])
        monkeypatch.setattr(courier, "_request", account)

        out = _save_answer("mug", "cube(1);")

        assert out["ok"] is True
        (row,) = out["auto_push"]["branches"]
        assert row["pushed"] is True and row["kept_as"] == "main@laptop"
        assert "moved on another computer" in out["auto_push"]["said"]

    def test_the_accounts_409_is_read_as_a_branch_that_moved(self, home, monkeypatch):
        import httpx

        monkeypatch.setenv("KILN_LICENSE_KEY", "kiln-test-key")
        monkeypatch.setattr(httpx, "request", lambda *a, **k: httpx.Response(409, json={
            "error": "branch diverged", "remote_head": "r", "local_parent": "p",
            "suggestion": "pull + rebase + push again"}))
        answer = courier._request("cloud_push_branch", "POST", "/api/cloud/designs/mug/branches/push", {})
        assert courier._diverged(answer)

    def test_the_command_says_where_the_work_went(self, home, monkeypatch):
        from click.testing import CliRunner

        import kiln.history_courier as hc
        from kiln.cli.history_commands import cloud_group

        monkeypatch.setattr(hc, "push", lambda *a, **k: {
            "status": "success", "kept_as": "main@laptop",
            "said": "The main branch of mug in your Kiln account moved on another computer."})

        out = CliRunner().invoke(cloud_group, ["push", "mug"])

        assert out.exit_code == 0 and "moved on another computer" in out.output
