"""Deleting a design from a computer that keeps its own copy.

Kiln's servers hold only the copy in the person's Kiln account.  The copy on
this computer is removed here, and only when the person asked for it: by
default a delete archives, ``where`` names the place a permanent delete
removes from, and the reply says what happened in each place.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiln import served_delete
from kiln.history_vault import HistoryVault, design_locked

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


def _recorded(design_id: str) -> list[str]:
    vault = HistoryVault()
    try:
        vault.sync_from_library(design_id)
        return sorted(vault.version_ids(design_id))
    finally:
        vault.close()


class _Servers:
    """Kiln's servers, as the delete tool answers there."""

    def __init__(self, *, held: list[str] | None = None, offline: bool = False) -> None:
        self.held = held
        self.offline = offline
        self.calls: list[dict] = []

    def __call__(self, **call):
        self.calls.append(call)
        if self.offline:
            return {"status": "error", "success": False, "why": "offline",
                    "error": "This computer is offline, so Kiln could not ask its servers."}
        design_id, where = call["design_id"], call.get("where") or "everywhere"
        facts = {"held": True, "versions": len(self.held or []), "version_ids": list(self.held or [])}
        if self.held is None:
            return {"status": "error", "code": "NOT_FOUND", "in_your_account": {"held": False},
                    "error": f"No design '{design_id}' in your Kiln account."}
        if where == "computer":
            return {"status": "error", "code": "NO_COMPUTER_COPY_HERE", "in_your_account": facts,
                    "error": "This request reached Kiln's servers."}
        if call.get("confirm") != design_id:
            return {"status": "success", "confirm_required": True, "in_your_account": facts,
                    "action": "permanent_delete" if call.get("permanent") else "retire",
                    "reversible": not call.get("permanent"), "confirm_with": design_id}
        done = "erased" if call.get("permanent") else "archived"
        if call.get("permanent"):
            self.held = None
        return {"status": "success", "deleted": True, "reversible": not call.get("permanent"),
                "what_happened": {"account": done, "computer": "not touched"}}


def _delete(servers, **call):
    return served_delete.run("delete_design", call, servers)


class TestRemoveItFromThisComputer:
    def test_the_first_call_says_what_the_account_still_keeps(self, home):
        folder = _library(home, "mug", [MESH_V1, MESH_V2])
        ids = _recorded("mug")
        said = _delete(_Servers(held=ids), design_id="mug", permanent=True, where="computer")
        assert said["confirm_required"] is True and said["where"] == "computer"
        assert said["on_this_computer"]["versions"] == 2
        assert said["in_your_account"]["held"] is True and said["not_in_your_account"] == 0
        assert "account still keeps" in said["message"]
        assert folder.is_dir()

    def test_versions_never_saved_to_the_account_are_named(self, home):
        _library(home, "mug", [MESH_V1, MESH_V2])
        ids = _recorded("mug")
        said = _delete(_Servers(held=ids[:1]), design_id="mug", permanent=True, where="computer")
        assert said["not_in_your_account"] == 1
        assert "gone for good" in said["message"]

    def test_the_only_copy_is_said_to_be_the_only_copy(self, home):
        _library(home, "mug", [MESH_V1])
        _recorded("mug")
        said = _delete(_Servers(held=None), design_id="mug", permanent=True, where="computer")
        assert said["in_your_account"]["held"] is False
        assert "only copy" in said["message"]

    def test_offline_it_still_works_and_says_the_account_was_not_checked(self, home):
        folder = _library(home, "mug", [MESH_V1])
        _recorded("mug")
        said = _delete(_Servers(offline=True), design_id="mug", permanent=True, where="computer")
        assert said["confirm_required"] is True and said["in_your_account"] == {"held": None}
        assert "could not check" in said["message"]
        done = _delete(_Servers(offline=True), design_id="mug", permanent=True, where="computer", confirm="mug")
        assert done["deleted"] is True and not folder.exists()

    def test_confirmed_it_removes_the_files_and_the_records_here_only(self, home):
        folder = _library(home, "mug", [MESH_V1, MESH_V2])
        other = _library(home, "vase", [MESH_V1])
        ids = _recorded("mug")
        _recorded("vase")
        servers = _Servers(held=ids)
        done = _delete(servers, design_id="mug", permanent=True, where="computer", confirm="mug")
        assert done["status"] == "success" and done["deleted"] is True
        assert done["what_happened"] == {"computer": "erased", "account": "not touched"}
        assert "kiln cloud clone" in done["bring_back"]
        assert not folder.exists() and other.is_dir()
        vault = HistoryVault()
        try:
            assert not vault.has_design("mug") and vault.version_ids("mug") == set()
            assert vault.has_design("vase")
        finally:
            vault.close()
        # The servers were asked what the account holds, and to change nothing.
        assert all(c.get("where") == "computer" for c in servers.calls)
        assert servers.held == ids

    def test_without_permanent_nothing_is_removed(self, home):
        folder = _library(home, "mug", [MESH_V1])
        said = _delete(_Servers(held=[]), design_id="mug", where="computer", confirm="mug")
        assert said["code"] == "WHERE_NEEDS_PERMANENT" and folder.is_dir()

    def test_a_design_this_computer_does_not_keep_is_not_found(self, home):
        said = _delete(_Servers(held=["v"]), design_id="mug", permanent=True, where="computer", confirm="mug")
        assert said["code"] == "NOT_FOUND" and "this computer" in said["error"]

    def test_a_design_id_naming_a_path_removes_nothing(self, home):
        (home / ".kiln" / "designs").mkdir(parents=True)
        precious = home / "precious"
        precious.mkdir()
        said = _delete(_Servers(held=None), design_id="../../precious", permanent=True,
                       where="computer", confirm="../../precious")
        assert said["code"] == "NOT_FOUND" and precious.is_dir()


class TestRemoveItFromMyAccount:
    def test_the_copy_here_is_kept_and_locked_to_this_computer(self, home):
        folder = _library(home, "mug", [MESH_V1])
        ids = _recorded("mug")
        done = _delete(_Servers(held=ids), design_id="mug", permanent=True, where="account", confirm="mug")
        assert done["deleted"] is True and folder.is_dir()
        assert design_locked("mug") is True
        assert "kept" in done["what_happened"]["computer"] and "locked" in done["what_happened"]["computer"]

    def test_a_refusal_changes_nothing_here(self, home):
        _library(home, "mug", [MESH_V1])
        _recorded("mug")
        said = _delete(_Servers(offline=True), design_id="mug", permanent=True, where="account", confirm="mug")
        assert said.get("why") == "offline" and design_locked("mug") is False


class TestDeleteItEverywhere:
    def test_the_default_archives_in_both_places(self, home):
        folder = _library(home, "mug", [MESH_V1])
        ids = _recorded("mug")
        done = _delete(_Servers(held=ids), design_id="mug", confirm="mug")
        assert done["deleted"] is True and done["what_happened"]["computer"] == "archived"
        assert json.loads((folder / ".kiln_recipe.json").read_text())["archived"] is True

    def test_scrapping_it_erases_both(self, home):
        folder = _library(home, "mug", [MESH_V1])
        ids = _recorded("mug")
        servers = _Servers(held=ids)
        done = _delete(servers, design_id="mug", permanent=True, confirm="mug")
        assert done["deleted"] is True and servers.held is None and not folder.exists()
        assert done["what_happened"] == {"account": "erased", "computer": "erased"}
        assert done["bring_back"].startswith("Nothing")

    def test_the_first_call_says_what_is_here_too(self, home):
        _library(home, "mug", [MESH_V1, MESH_V2])
        ids = _recorded("mug")
        said = _delete(_Servers(held=ids), design_id="mug", permanent=True)
        assert said["confirm_required"] is True and said["on_this_computer"]["versions"] == 2

    def test_a_design_only_on_this_computer_is_scrapped_here(self, home):
        folder = _library(home, "mug", [MESH_V1])
        _recorded("mug")
        said = _delete(_Servers(held=None), design_id="mug", permanent=True)
        assert said["confirm_required"] is True and "only copy" in said["message"]
        done = _delete(_Servers(held=None), design_id="mug", permanent=True, confirm="mug")
        assert done["deleted"] is True and not folder.exists()
        assert done["what_happened"]["account"] == "not there"

    def test_when_the_servers_cannot_be_asked_nothing_is_erased_anywhere(self, home):
        folder = _library(home, "mug", [MESH_V1])
        _recorded("mug")
        said = _delete(_Servers(offline=True), design_id="mug", permanent=True, confirm="mug")
        assert said.get("why") == "offline" and folder.is_dir()
        assert "where='computer'" in said["this_computer"]

    def test_a_design_in_neither_place_is_the_servers_not_found(self, home):
        said = _delete(_Servers(held=None), design_id="mug", confirm="mug")
        assert said["code"] == "NOT_FOUND"
