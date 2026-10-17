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
