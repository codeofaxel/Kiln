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
    recipe_path_for_version,
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


class TestRecipeFileOfAVersion:
    def test_found_by_the_id_inside_not_the_number_in_the_name(self, tmp_path):
        _write(tmp_path, ".kiln_recipe.v2.json", version=2, version_id="first")
        wanted = _write(tmp_path, ".kiln_recipe.v3.json", version=3, version_id="second")
        # A record that calls "second" number 2 must still get second's file.
        assert recipe_path_for_version(tmp_path, "second", 2) == wanted

    def test_a_number_only_answers_for_a_file_that_names_no_version(self, tmp_path):
        _write(tmp_path, ".kiln_recipe.v2.json", version=2, version_id="first")
        assert recipe_path_for_version(tmp_path, "unknown", 2) is None
        bare = _write(tmp_path, ".kiln_recipe.v1.json", version=1)
        assert recipe_path_for_version(tmp_path, "unknown", 1) == bare

    def test_the_head_is_never_a_kept_version(self, tmp_path):
        _write(tmp_path, ".kiln_recipe.json", version=1, version_id="first")
        assert recipe_path_for_version(tmp_path, "first", 1) is None


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
