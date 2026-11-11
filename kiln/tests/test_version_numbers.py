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
    """A number is given once per design, whatever the folder holds."""

    def test_a_save_with_no_current_recipe_is_numbered_past_the_kept_files(self, tmp_path, monkeypatch):
        """A design with kept files and no current recipe (a pulled one):
        the save is numbered past them and writes over none of them."""
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
        from kiln.plugins import version_tools

        monkeypatch.setattr(version_tools, "_DESIGNS_ROOT", str(tmp_path))

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


class TestTheStoreNeverRepeatsANumber:
    """The history record gives each number once, from every door."""

    def test_a_deleted_versions_number_is_not_given_again(self, tmp_path):
        store = DesignVersionStore(db_path=str(tmp_path / "v.db"))
        try:
            store.save_version("part", "cube(1);")
            second = store.save_version("part", "cube(2);")
            assert store.delete_version(second.version_id)
            assert store.save_version("part", "cube(3);").version_number == 3
            assert store.numbers("part") == {1, 3}
        finally:
            store.close()

    def test_saves_from_many_processes_never_share_a_number(self, tmp_path):
        import subprocess
        import sys

        db = str(tmp_path / "v.db")
        DesignVersionStore(db_path=db).close()
        code = (
            "import sys; from kiln.design_versions import DesignVersionStore as S\n"
            "s = S(db_path=sys.argv[1])\n"
            "print(*[s.save_version('part', 'cube(1);').version_number for _ in range(5)])\n"
        )
        import os

        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        procs = [subprocess.Popen([sys.executable, "-c", code, db], stdout=subprocess.PIPE, env=env) for _ in range(6)]
        got = sorted(int(n) for p in procs for n in p.communicate()[0].split())
        assert got == list(range(1, 31))

    def test_a_version_with_no_recipe_is_numbered_past_the_recipes_and_noted(self, tmp_path, monkeypatch):
        from kiln.plugins import version_tools
        from kiln.version_numbers import next_version_number

        monkeypatch.setattr(version_tools, "_DESIGNS_ROOT", str(tmp_path / "designs"))
        folder = tmp_path / "designs" / "part"
        folder.mkdir(parents=True)
        _write(folder, ".kiln_recipe.v4.json", version=4, version_id="kept")
        store = DesignVersionStore(db_path=str(tmp_path / "v.db"))
        try:
            assert store.save_without_recipe("part", "cube(1);").version_number == 5
        finally:
            store.close()
        assert next_version_number(folder) == 6

    def test_a_huge_number_is_damage_not_a_count(self, tmp_path):
        (tmp_path / ".kiln_numbers.json").write_text(json.dumps({"highest": 10**30}))
        _write(tmp_path, ".kiln_recipe.v2.json", version=2)
        assert next_version_number(tmp_path) == 3

    def test_a_recipe_outside_the_library_takes_no_designs_numbers(self, tmp_path, monkeypatch):
        from kiln import design_versions
        from kiln.plugins import version_tools

        monkeypatch.setattr(version_tools, "_DESIGNS_ROOT", str(tmp_path / "designs"))
        db = str(tmp_path / "v.db")
        monkeypatch.setattr(design_versions, "_DEFAULT_DB_PATH", db)
        store = DesignVersionStore(db_path=db)
        try:
            for _ in range(7):
                store.save_version("part", "cube(1);")
        finally:
            store.close()
        elsewhere = tmp_path / "desktop" / "part"
        elsewhere.mkdir(parents=True)
        head = _write(elsewhere, ".kiln_recipe.json", version=1)
        assert create_new_version(load_recipe(str(head)), str(head)).version == 2


# --- each test below fails on the code before the lock every door holds ---


class TestOneLockForEveryDoor:
    def test_a_recipe_door_and_a_store_only_door_never_share_a_number(self, tmp_path):
        """A store-only save racing a save that writes a recipe: both hold
        the folder's numbers lock while they read the numbers and take one,
        so every version gets its own."""
        import os
        import subprocess
        import sys

        designs = tmp_path / "designs"
        (designs / "part").mkdir(parents=True)
        db = str(tmp_path / "v.db")
        DesignVersionStore(db_path=db).close()
        common = (
            "import sys, json\n"
            "from kiln import design_versions\n"
            "from kiln.plugins import version_tools\n"
            "version_tools._DESIGNS_ROOT = sys.argv[1]\n"
            "design_versions._DEFAULT_DB_PATH = sys.argv[2]\n"
        )
        recipe_door = common + (
            "from pathlib import Path\n"
            "from kiln.version_numbers import reserve_version_number, numbers_in_store\n"
            "folder = Path(sys.argv[1]) / 'part'\n"
            "got = []\n"
            "for _ in range(8):\n"
            "    n = reserve_version_number(folder, also=lambda: numbers_in_store('part'))\n"
            "    s = design_versions.DesignVersionStore()\n"
            "    s.save_version('part', 'cube(1);', version_number=n)\n"
            "    s.close(); got.append(n)\n"
            "print(*got)\n"
        )
        store_door = common + (
            "s = design_versions.DesignVersionStore()\n"
            "print(*[s.save_without_recipe('part', 'cube(2);').version_number for _ in range(8)])\n"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        procs = [
            subprocess.Popen([sys.executable, "-c", code, str(designs), db], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env)
            for code in (recipe_door, store_door, recipe_door, store_door)
        ]
        outs = [p.communicate() for p in procs]
        assert all(p.returncode == 0 for p in procs), [o[1][-400:] for o in outs]
        got = sorted(int(n) for out, _err in outs for n in out.split())
        assert got == list(range(1, 33))
        store = DesignVersionStore(db_path=db)
        try:
            rows = store.list_versions("part", limit=100)
        finally:
            store.close()
        assert sorted(v.version_number for v in rows) == list(range(1, 33))

    def test_a_save_that_cannot_get_the_lock_is_refused_not_let_through(self, tmp_path, monkeypatch):
        """A save that cannot get the lock is refused and takes no number;
        it never runs beside a save that holds it."""
        import fcntl

        from kiln import version_numbers

        monkeypatch.setattr(version_numbers, "_LOCK_WAIT_SECONDS", 0.2)
        _write(tmp_path, ".kiln_recipe.v1.json", version=1)
        holder = open(tmp_path / ".kiln_numbers.lock", "a+")
        try:
            fcntl.flock(holder, fcntl.LOCK_EX)
            with pytest.raises(version_numbers.NumbersBusy):
                version_numbers.reserve_version_number(tmp_path)
        finally:
            holder.close()
        assert not (tmp_path / ".kiln_numbers.json").exists(), "a refused save takes no number"
        assert version_numbers.reserve_version_number(tmp_path) == 2


class TestTheStoreRefusesANumberAlreadyHeld:
    def test_a_held_number_is_refused(self, tmp_path):
        store = DesignVersionStore(db_path=str(tmp_path / "v.db"))
        try:
            first = store.save_version("part", "cube(1);")
            with pytest.raises(ValueError, match="already held"):
                store.save_version("part", "cube(2);", version_number=first.version_number)
            with pytest.raises(ValueError):
                store.save_version("part", "cube(3);", version_number=10**12)
            assert [v.version_number for v in store.list_versions("part", limit=10)] == [1]
        finally:
            store.close()


class TestDamageNeverCarriesAnAbsurdNumber:
    def test_a_huge_number_in_a_file_name_is_ignored(self, tmp_path):
        _write(tmp_path, ".kiln_recipe.v99999999999999999999.json", version=3)
        _write(tmp_path, ".kiln_recipe.v2.json", version=2)
        assert numbers_in_folder(tmp_path) == {2, 3}
        assert next_version_number(tmp_path) == 4


class TestNothingIsWrittenOutsideTheLibrary:
    def test_a_recipe_outside_the_library_is_numbered_without_writing_beside_it(self, tmp_path, monkeypatch):
        from kiln.plugins import version_tools

        monkeypatch.setattr(version_tools, "_DESIGNS_ROOT", str(tmp_path / "designs"))
        project = tmp_path / "my-project"
        project.mkdir()
        head = _write(project, ".kiln_recipe.json", version=1, design_id="part")
        _write(project, ".kiln_recipe.v1.json", version=1, design_id="part")
        assert create_new_version(load_recipe(str(head)), str(head)).version == 2
        assert sorted(p.name for p in project.iterdir()) == [".kiln_recipe.json", ".kiln_recipe.v1.json"]


class TestASaveAfterTheCallersOwnWrite:
    def test_an_open_transaction_on_the_connection_does_not_fail_the_save(self, tmp_path):
        """A caller's uncommitted write on the store's connection is kept,
        and the save after it succeeds."""
        store = DesignVersionStore(db_path=str(tmp_path / "v.db"))
        try:
            first = store.save_version("part", "cube(1);")
            store._conn.execute("UPDATE design_versions SET notes = 'edited' WHERE version_id = ?", (first.version_id,))
            assert store.save_version("part", "cube(2);").version_number == 2
            assert store.get_version(first.version_id).notes == "edited"
        finally:
            store.close()
