"""A design version's number is given once, by one function."""

from __future__ import annotations

import json

import pytest

from kiln.design_recipe import create_new_version, create_recipe, load_recipe, save_recipe
from kiln.design_versions import DesignVersionStore
from kiln.version_numbers import (
    holds_a_version,
    next_version_number,
    numbers_in_folder,
)


def _write(folder, name, **fields):
    path = folder / name
    path.write_text(json.dumps({"name": "part", "created": "2026-01-01T00:00:00+00:00", **fields}))
    return path


class TestNextVersionNumber:
    def test_an_empty_folder_starts_at_one(self, tmp_path):
        assert next_version_number(tmp_path) == 1

    def test_a_slot_with_no_version_takes_no_number(self, tmp_path):
        slot = _write(tmp_path, ".kiln_recipe.json", design_id="part")
        assert not holds_a_version(slot)
        assert next_version_number(tmp_path) == 1

    def test_one_past_the_highest_wherever_it_is_recorded(self, tmp_path):
        _write(tmp_path, ".kiln_recipe.v2.json", version=2)
        _write(tmp_path, ".kiln_recipe.abcdef123456.json", version=5, version_id="abcdef1234567890")
        assert numbers_in_folder(tmp_path) == {2, 5}
        assert next_version_number(tmp_path) == 6
        assert next_version_number(tmp_path, [9, None, True, "x"]) == 10


class TestTheFirstSaveIntoASlotIsVersionOne:
    def test_create_new_version_from_a_slot(self, tmp_path):
        slot = _write(tmp_path, ".kiln_recipe.json", design_id="part")
        first = create_new_version(load_recipe(str(slot)), str(slot))
        assert first.version == 1
        save_recipe(first, str(tmp_path))
        second = create_new_version(load_recipe(str(slot)), str(slot))
        assert second.version == 2

    def test_a_version_is_numbered_past_every_kept_version(self, tmp_path):
        """A head that is an older version (a rollback, a pulled branch)
        does not number the next save over a newer version's file."""
        recipe = create_recipe("part", [])
        save_recipe(recipe, str(tmp_path))
        _write(tmp_path, ".kiln_recipe.v4.json", version=4, version_id="other")
        head = tmp_path / ".kiln_recipe.json"
        assert create_new_version(load_recipe(str(head)), str(head)).version == 5


class TestTheStoreKeepsTheNumberItIsGiven:
    def test_given_number_is_kept_and_the_next_follows_it(self, tmp_path):
        store = DesignVersionStore(db_path=str(tmp_path / "v.db"))
        try:
            assert store.save_version("part", "cube(1);").version_number == 1
            assert store.save_version("part", "cube(2);", version_number=7).version_number == 7
            assert store.save_version("part", "cube(3);").version_number == 8
            with pytest.raises(ValueError):
                store.save_version("part", "cube(4);", version_number=0)
        finally:
            store.close()


class TestTheStoreAndTheRecipesShareOneCount:
    """A door that writes only the store, and a door that writes only a
    recipe, never hand out the same number."""

    def test_a_recipe_is_numbered_past_the_store(self, tmp_path, monkeypatch):
        from kiln import design_versions
        from kiln.version_numbers import numbers_in_store

        db = str(tmp_path / "v.db")
        monkeypatch.setattr(design_versions, "_DEFAULT_DB_PATH", db)
        assert numbers_in_store("part") == set()
        assert not (tmp_path / "v.db").exists(), "asking for a number creates no store"
        store = DesignVersionStore(db_path=db)
        try:
            store.save_version("part", "cube(1);")
            store.save_version("part", "cube(2);")
        finally:
            store.close()
        assert numbers_in_store("part") == {1, 2}
        folder = tmp_path / "part"
        folder.mkdir()
        recipe = create_recipe("part", [])
        recipe.design_id = "part"
        save_recipe(recipe, str(folder))
        head = str(folder / ".kiln_recipe.json")
        assert create_new_version(load_recipe(head), head).version == 3

    def test_the_store_is_numbered_past_the_recipes(self, tmp_path, monkeypatch):
        from kiln.plugins import version_tools

        monkeypatch.setattr(version_tools, "_DESIGNS_ROOT", str(tmp_path / "designs"))
        folder = tmp_path / "designs" / "part"
        folder.mkdir(parents=True)
        _write(folder, ".kiln_recipe.v4.json", version=4, version_id="kept")
        store = DesignVersionStore(db_path=str(tmp_path / "v.db"))
        try:
            assert store.next_number("part") == 5
            # A caller that wrote the recipe first hands its number in; the
            # store's own count never reads that recipe as a second version.
            assert store.save_version("part", "cube(1);", version_number=4).version_number == 4
            assert store.save_version("part", "cube(2);").version_number == 5
        finally:
            store.close()


class TestANumberIsNeverGivenTwice:
    """Findings of an independent review of the first cut."""

    def test_a_save_with_no_current_recipe_is_numbered_past_the_kept_files(self, tmp_path, monkeypatch):
        """A pulled design has kept files and no current recipe; the save
        into it was numbered 1 and written over the pulled version 1."""
        from kiln.plugins import version_tools

        monkeypatch.setattr(version_tools, "_DESIGNS_ROOT", str(tmp_path))
        import kiln.server as srv

        monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
        folder = tmp_path / "pulled"
        folder.mkdir()
        for n in (1, 2, 3):
            _write(folder, f".kiln_recipe.v{n}.json", version=n, version_id=f"pulled-{n}", notes=f"pulled {n}")
        captured = {}

        class _MCP:
            def tool(self, **_kw):
                def deco(fn):
                    captured[fn.__name__] = fn
                    return fn

                return deco

        version_tools._VersionToolsPlugin().register(_MCP())
        reply = captured["save_design_version"](design_id="pulled", scad_source="cube(1);", notes="mine")
        saved = reply["version"] if isinstance(reply, dict) else reply
        assert json.loads((folder / ".kiln_recipe.v1.json").read_text())["notes"] == "pulled 1"
        assert json.loads((folder / ".kiln_recipe.v4.json").read_text())["notes"] == "mine"
        assert saved["version"] == 4

    def test_a_removed_version_does_not_free_its_number(self, tmp_path):
        from kiln.version_numbers import reserve_version_number

        _write(tmp_path, ".kiln_recipe.v1.json", version=1)
        assert reserve_version_number(tmp_path) == 2
        _write(tmp_path, ".kiln_recipe.v2.json", version=2)
        (tmp_path / ".kiln_recipe.v2.json").unlink()
        assert reserve_version_number(tmp_path) == 3

    def test_two_takers_never_get_one_number(self, tmp_path):
        from concurrent.futures import ThreadPoolExecutor

        from kiln.version_numbers import reserve_version_number

        with ThreadPoolExecutor(8) as pool:
            got = list(pool.map(lambda _i: reserve_version_number(tmp_path), range(64)))
        assert sorted(got) == list(range(1, 65))

    def test_a_file_that_cannot_be_read_still_holds_its_number(self, tmp_path):
        (tmp_path / ".kiln_recipe.v3.json").write_text("{not json")
        _write(tmp_path, ".kiln_recipe.v2.json", version="two")
        assert numbers_in_folder(tmp_path) == {2, 3}
        assert next_version_number(tmp_path) == 4

    def test_a_recipe_naming_no_design_still_reads_the_store(self, tmp_path, monkeypatch):
        from kiln import design_versions

        db = str(tmp_path / "v.db")
        monkeypatch.setattr(design_versions, "_DEFAULT_DB_PATH", db)
        store = DesignVersionStore(db_path=db)
        try:
            store.save_version("part", "cube(1);")
            store.save_version("part", "cube(2);")
        finally:
            store.close()
        folder = tmp_path / "part"
        folder.mkdir()
        head = _write(folder, ".kiln_recipe.json", version=1)  # no design_id inside
        _write(folder, ".kiln_recipe.v1.json", version=1)
        assert create_new_version(load_recipe(str(head)), str(head)).version == 3


class TestNoSharedStoreIsReadOnASharedServer:
    def test_the_default_store_is_not_opened(self, tmp_path, monkeypatch):
        """One default file would be every customer's: with nothing making
        the store class open only the caller's own copy, no store is read."""
        from kiln import design_versions, runtime_env, version_numbers

        db = str(tmp_path / "v.db")
        monkeypatch.setattr(design_versions, "_DEFAULT_DB_PATH", db)
        store = DesignVersionStore(db_path=db)
        try:
            store.save_version("mug", "cube(1);")
        finally:
            store.close()
        assert version_numbers.numbers_in_store("mug") == {1}
        monkeypatch.setattr(runtime_env, "is_hosted_multitenant", lambda: True)
        assert version_numbers.numbers_in_store("mug") == set()
        assert version_numbers.numbers_in_store("mug", db) == set()
