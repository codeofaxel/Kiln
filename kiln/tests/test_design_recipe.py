"""Tests for kiln.design_recipe — design recipe system."""

from __future__ import annotations

import json
import os

import pytest

from kiln.design_recipe import (
    DesignPart,
    DesignRecipe,
    create_new_version,
    create_recipe,
    find_recipe,
    find_recipes_recursive,
    list_recipe_versions,
    load_recipe,
    save_recipe,
    update_parameter,
    update_part_color,
)


def _sample_parts() -> list[dict]:
    return [
        {
            "name": "body",
            "role": "structural",
            "stl_path": "/tmp/body.stl",
            "color": "white",
            "filament_slot": 0,
        },
        {
            "name": "portrait",
            "role": "decoration",
            "stl_path": "/tmp/portrait.stl",
            "color": "black",
            "filament_slot": 1,
            "slicer_profile": "0.20mm_standard",
            "gcode_path": "/tmp/portrait.gcode",
        },
    ]


def _sample_recipe() -> DesignRecipe:
    return create_recipe(
        "test-coaster",
        _sample_parts(),
        source_scad="/tmp/coaster.scad",
        parameters={"diameter": 80, "depth": 1.5},
        notes="test recipe",
    )


class TestDesignPart:
    """DesignPart dataclass: construction, serialization."""

    def test_to_dict(self):
        part = DesignPart(
            name="body",
            role="structural",
            stl_path="/tmp/body.stl",
            color="white",
        )
        d = part.to_dict()
        assert d["name"] == "body"
        assert d["role"] == "structural"
        assert d["filament_slot"] is None
        assert d["slicer_profile"] is None
        assert d["gcode_path"] is None

    def test_from_dict_minimal(self):
        part = DesignPart.from_dict(
            {"name": "qr", "role": "functional", "stl_path": "/tmp/qr.stl", "color": "#000000"}
        )
        assert part.name == "qr"
        assert part.filament_slot is None

    def test_from_dict_full(self):
        part = DesignPart.from_dict(_sample_parts()[1])
        assert part.filament_slot == 1
        assert part.slicer_profile == "0.20mm_standard"
        assert part.gcode_path == "/tmp/portrait.gcode"

    def test_round_trip(self):
        original = DesignPart(
            name="cap",
            role="functional",
            stl_path="/tmp/cap.stl",
            color="red",
            filament_slot=2,
            slicer_profile="fast",
            gcode_path="/tmp/cap.gcode",
        )
        restored = DesignPart.from_dict(original.to_dict())
        assert restored.name == original.name
        assert restored.filament_slot == original.filament_slot
        assert restored.gcode_path == original.gcode_path


class TestDesignRecipe:
    """DesignRecipe dataclass: construction, serialization, round-trip."""

    def test_to_dict_includes_all_fields(self):
        recipe = _sample_recipe()
        d = recipe.to_dict()
        assert d["name"] == "test-coaster"
        assert len(d["parts"]) == 2
        assert d["source_scad"] == "/tmp/coaster.scad"
        assert d["parameters"]["diameter"] == 80
        assert d["merge_order"] == ["body", "portrait"]
        assert d["notes"] == "test recipe"

    def test_from_dict_round_trip(self):
        original = _sample_recipe()
        restored = DesignRecipe.from_dict(original.to_dict())
        assert restored.name == original.name
        assert len(restored.parts) == len(original.parts)
        assert restored.parts[0].color == "white"
        assert restored.parameters == original.parameters
        assert restored.merge_order == original.merge_order

    def test_from_dict_missing_optional_fields(self):
        data = {"name": "bare", "created": "2026-01-01T00:00:00Z"}
        recipe = DesignRecipe.from_dict(data)
        assert recipe.parts == []
        assert recipe.parameters == {}
        assert recipe.merge_order == []
        assert recipe.final_3mf is None
        assert recipe.notes == ""

    def test_from_dict_missing_name_raises(self):
        with pytest.raises(KeyError):
            DesignRecipe.from_dict({"created": "2026-01-01T00:00:00Z"})

    def test_brief_id_and_intent_hash_round_trip(self, tmp_path):
        """When a recipe is produced from a brief lifecycle, brief_id
        and intent_hash MUST round-trip through to_dict → JSON → from_dict
        so downstream audits can verify the mesh against the same brief.
        """
        recipe = create_recipe(
            "brief-coaster",
            _sample_parts(),
            source_scad="/tmp/coaster.scad",
            parameters={"diameter": 80},
            notes="from-brief",
        )
        recipe.brief_id = "brief-abc123"
        recipe.intent_hash = "sha256:deadbeefcafe"

        d = recipe.to_dict()
        assert d["brief_id"] == "brief-abc123"
        assert d["intent_hash"] == "sha256:deadbeefcafe"

        path = save_recipe(recipe, str(tmp_path))
        with open(path) as fh:
            on_disk = json.load(fh)
        assert on_disk["brief_id"] == "brief-abc123"
        assert on_disk["intent_hash"] == "sha256:deadbeefcafe"

        loaded = load_recipe(path)
        assert loaded.brief_id == "brief-abc123"
        assert loaded.intent_hash == "sha256:deadbeefcafe"

    def test_brief_fields_default_none_and_absent_from_dict(self):
        """Recipes outside the brief lifecycle leave both fields unset
        AND keep them out of the serialized dict so on-disk JSON for
        legacy recipes stays byte-identical.
        """
        recipe = _sample_recipe()
        assert recipe.brief_id is None
        assert recipe.intent_hash is None
        d = recipe.to_dict()
        assert "brief_id" not in d
        assert "intent_hash" not in d


class TestSaveLoad:
    """Filesystem save/load operations."""

    def test_save_and_load_round_trip(self, tmp_path):
        recipe = _sample_recipe()
        path = save_recipe(recipe, str(tmp_path))
        assert path.endswith(".kiln_recipe.json")
        assert os.path.isfile(path)

        loaded = load_recipe(path)
        assert loaded.name == recipe.name
        assert len(loaded.parts) == 2
        assert loaded.parameters["depth"] == 1.5

    def test_save_writes_valid_json(self, tmp_path):
        recipe = _sample_recipe()
        path = save_recipe(recipe, str(tmp_path))
        with open(path) as fh:
            data = json.load(fh)
        assert data["name"] == "test-coaster"

    def test_save_nonexistent_directory_raises(self):
        recipe = _sample_recipe()
        with pytest.raises(FileNotFoundError, match="does not exist"):
            save_recipe(recipe, "/nonexistent/directory/xyz")

    def test_load_nonexistent_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_recipe("/nonexistent/file.json")

    def test_load_invalid_json_raises(self, tmp_path):
        bad_file = tmp_path / ".kiln_recipe.json"
        bad_file.write_text("not json{{{")
        with pytest.raises(json.JSONDecodeError):
            load_recipe(str(bad_file))

    def test_instance_save_method(self, tmp_path):
        recipe = _sample_recipe()
        path = recipe.save(str(tmp_path))
        assert os.path.isfile(path)

    def test_instance_load_classmethod(self, tmp_path):
        recipe = _sample_recipe()
        path = save_recipe(recipe, str(tmp_path))
        loaded = DesignRecipe.load(path)
        assert loaded.name == recipe.name


class TestFindRecipe:
    """find_recipe and find_recipes_recursive."""

    def test_find_recipe_exists(self, tmp_path):
        recipe = _sample_recipe()
        save_recipe(recipe, str(tmp_path))
        assert find_recipe(str(tmp_path)) is not None

    def test_find_recipe_missing(self, tmp_path):
        assert find_recipe(str(tmp_path)) is None

    def test_find_recipe_nonexistent_dir(self):
        assert find_recipe("/nonexistent/path") is None

    def test_find_recipes_recursive(self, tmp_path):
        sub1 = tmp_path / "design_a"
        sub2 = tmp_path / "design_b"
        sub1.mkdir()
        sub2.mkdir()
        save_recipe(_sample_recipe(), str(sub1))
        save_recipe(_sample_recipe(), str(sub2))
        results = find_recipes_recursive(str(tmp_path))
        assert len(results) == 2

    def test_find_recipes_recursive_empty(self, tmp_path):
        assert find_recipes_recursive(str(tmp_path)) == []

    def test_find_recipes_recursive_nonexistent_dir(self):
        assert find_recipes_recursive("/nonexistent") == []


class TestUpdatePartColor:
    """update_part_color: color and slot changes."""

    def test_update_color(self):
        recipe = _sample_recipe()
        updated = update_part_color(recipe, "body", "grey")
        assert updated.parts[0].color == "grey"
        # Original unchanged (deep copy)
        assert recipe.parts[0].color == "white"

    def test_update_color_and_slot(self):
        recipe = _sample_recipe()
        updated = update_part_color(recipe, "portrait", "red", new_slot=3)
        assert updated.parts[1].color == "red"
        assert updated.parts[1].filament_slot == 3

    def test_update_color_unknown_part_raises(self):
        recipe = _sample_recipe()
        with pytest.raises(ValueError, match="not found"):
            update_part_color(recipe, "nonexistent", "blue")


class TestUpdateParameter:
    """update_parameter: OpenSCAD parameter changes."""

    def test_update_existing_param(self):
        recipe = _sample_recipe()
        updated = update_parameter(recipe, "diameter", 100)
        assert updated.parameters["diameter"] == 100
        # Original unchanged
        assert recipe.parameters["diameter"] == 80

    def test_add_new_param(self):
        recipe = _sample_recipe()
        updated = update_parameter(recipe, "border_width", 2.0)
        assert updated.parameters["border_width"] == 2.0
        assert "border_width" not in recipe.parameters

    def test_empty_param_name_raises(self):
        recipe = _sample_recipe()
        with pytest.raises(ValueError, match="must not be empty"):
            update_parameter(recipe, "", 42)


class TestCreateRecipe:
    """create_recipe convenience factory."""

    def test_creates_with_defaults(self):
        recipe = create_recipe("minimal", [_sample_parts()[0]])
        assert recipe.name == "minimal"
        assert recipe.created  # non-empty ISO timestamp
        assert recipe.merge_order == ["body"]
        assert recipe.notes == ""
        assert recipe.parameters == {}

    def test_empty_parts_list(self):
        recipe = create_recipe("empty", [])
        assert recipe.parts == []
        assert recipe.merge_order == []


class TestVersionFields:
    """Version tracking fields on DesignRecipe."""

    def test_default_version_is_one(self):
        recipe = _sample_recipe()
        assert recipe.version == 1
        assert recipe.parent_version is None
        assert recipe.changes is None

    def test_to_dict_omits_none_version_fields(self):
        recipe = _sample_recipe()
        d = recipe.to_dict()
        assert d["version"] == 1
        assert "parent_version" not in d
        assert "changes" not in d

    def test_to_dict_includes_version_fields_when_set(self):
        recipe = _sample_recipe()
        recipe.version = 3
        recipe.parent_version = "/tmp/.kiln_recipe.v2.json"
        recipe.changes = {"body.color": "white -> red"}
        d = recipe.to_dict()
        assert d["version"] == 3
        assert d["parent_version"] == "/tmp/.kiln_recipe.v2.json"
        assert d["changes"] == {"body.color": "white -> red"}

    def test_from_dict_with_version_fields(self):
        data = {
            "name": "test",
            "created": "2026-01-01T00:00:00Z",
            "version": 5,
            "parent_version": "/tmp/parent.json",
            "changes": {"portrait.color": "black -> white"},
        }
        recipe = DesignRecipe.from_dict(data)
        assert recipe.version == 5
        assert recipe.parent_version == "/tmp/parent.json"
        assert recipe.changes == {"portrait.color": "black -> white"}

    def test_from_dict_legacy_no_version(self):
        """Recipes saved before versioning should default to v1."""
        data = {"name": "legacy", "created": "2025-01-01T00:00:00Z"}
        recipe = DesignRecipe.from_dict(data)
        assert recipe.version == 1
        assert recipe.parent_version is None
        assert recipe.changes is None


class TestCreateNewVersion:
    """create_new_version: version increment, parent tracking, change delta."""

    def test_increments_version(self, tmp_path):
        parent = _sample_recipe()
        parent_path = save_recipe(parent, str(tmp_path))
        new = create_new_version(parent, parent_path)
        assert new.version == 2
        assert new.parent_version == os.path.abspath(parent_path)

    def test_records_changes(self, tmp_path):
        parent = _sample_recipe()
        parent_path = save_recipe(parent, str(tmp_path))
        changes = {"body.color": "white -> red"}
        new = create_new_version(parent, parent_path, changes=changes, notes="recolor")
        assert new.changes == changes
        assert new.notes == "recolor"

    def test_deep_copies_parts(self, tmp_path):
        parent = _sample_recipe()
        parent_path = save_recipe(parent, str(tmp_path))
        new = create_new_version(parent, parent_path)
        new.parts[0].color = "red"
        assert parent.parts[0].color == "white"  # original unchanged

    def test_invalidates_final_3mf(self, tmp_path):
        parent = _sample_recipe()
        parent.final_3mf = "/tmp/old.3mf"
        parent_path = save_recipe(parent, str(tmp_path))
        new = create_new_version(parent, parent_path)
        assert new.final_3mf is None

    def test_fresh_timestamp(self, tmp_path):
        parent = _sample_recipe()
        # Pin the parent to a known past timestamp.  Reading the wall
        # clock twice in quick succession can return the identical
        # value on platforms with coarse clock resolution (Windows),
        # so a fresh ``create_new_version()`` timestamp must be
        # verified against a deterministically older parent value.
        parent.created = "2000-01-01T00:00:00+00:00"
        parent_path = save_recipe(parent, str(tmp_path))
        new = create_new_version(parent, parent_path)
        assert new.created != parent.created

    def test_chain_three_versions(self, tmp_path):
        v1 = _sample_recipe()
        p1 = save_recipe(v1, str(tmp_path))
        v2 = create_new_version(v1, p1, changes={"body.color": "white -> red"})
        p2 = save_recipe(v2, str(tmp_path))
        v3 = create_new_version(v2, p2, changes={"portrait.color": "black -> blue"})
        assert v3.version == 3
        assert v3.parent_version == os.path.abspath(p2)


class TestSaveRecipeVersioning:
    """save_recipe now writes versioned snapshot files."""

    def test_writes_versioned_file(self, tmp_path):
        recipe = _sample_recipe()
        save_recipe(recipe, str(tmp_path))
        versioned = tmp_path / ".kiln_recipe.v1.json"
        assert versioned.exists()

    def test_versioned_file_matches_main(self, tmp_path):
        recipe = _sample_recipe()
        save_recipe(recipe, str(tmp_path))
        main = json.loads((tmp_path / ".kiln_recipe.json").read_text())
        versioned = json.loads((tmp_path / ".kiln_recipe.v1.json").read_text())
        assert main == versioned

    def test_multiple_versions_on_disk(self, tmp_path):
        v1 = _sample_recipe()
        p1 = save_recipe(v1, str(tmp_path))
        v2 = create_new_version(v1, p1, changes={"body.color": "white -> red"})
        v2.parts[0].color = "red"
        save_recipe(v2, str(tmp_path))
        assert (tmp_path / ".kiln_recipe.v1.json").exists()
        assert (tmp_path / ".kiln_recipe.v2.json").exists()
        # Main file should be v2
        main = json.loads((tmp_path / ".kiln_recipe.json").read_text())
        assert main["version"] == 2


class TestProvenanceFields:
    """Provenance fields: design_id, prompt, generation_provider, provenance, stl_path."""

    def test_defaults_are_none(self):
        recipe = _sample_recipe()
        assert recipe.design_id is None
        assert recipe.prompt is None
        assert recipe.generation_provider is None
        assert recipe.provenance is None
        assert recipe.stl_path is None

    def test_to_dict_omits_none_provenance_fields(self):
        recipe = _sample_recipe()
        d = recipe.to_dict()
        assert "design_id" not in d
        assert "prompt" not in d
        assert "generation_provider" not in d
        assert "provenance" not in d
        assert "stl_path" not in d

    def test_to_dict_includes_provenance_fields_when_set(self):
        recipe = _sample_recipe()
        recipe.design_id = "ash-coaster"
        recipe.prompt = "portrait coaster with embossed face"
        recipe.generation_provider = "openscad"
        recipe.provenance = {"tools_used": ["rembg", "openscad"], "source_files": ["/tmp/ash.png"]}
        recipe.stl_path = "/tmp/coaster.stl"
        d = recipe.to_dict()
        assert d["design_id"] == "ash-coaster"
        assert d["prompt"] == "portrait coaster with embossed face"
        assert d["generation_provider"] == "openscad"
        assert d["provenance"]["tools_used"] == ["rembg", "openscad"]
        assert d["stl_path"] == "/tmp/coaster.stl"

    def test_from_dict_round_trip_with_provenance(self):
        recipe = _sample_recipe()
        recipe.design_id = "ash-coaster"
        recipe.prompt = "embossed portrait"
        recipe.generation_provider = "gemini"
        recipe.provenance = {"change_summary": "initial generation"}
        recipe.stl_path = "/tmp/merged.stl"
        restored = DesignRecipe.from_dict(recipe.to_dict())
        assert restored.design_id == "ash-coaster"
        assert restored.prompt == "embossed portrait"
        assert restored.generation_provider == "gemini"
        assert restored.provenance == {"change_summary": "initial generation"}
        assert restored.stl_path == "/tmp/merged.stl"

    def test_backward_compat_old_dict_without_provenance(self):
        """Old recipes without provenance fields must load with None defaults."""
        data = {"name": "legacy", "created": "2025-01-01T00:00:00Z"}
        recipe = DesignRecipe.from_dict(data)
        assert recipe.design_id is None
        assert recipe.prompt is None
        assert recipe.generation_provider is None
        assert recipe.provenance is None
        assert recipe.stl_path is None

    def test_create_new_version_carries_provenance(self, tmp_path):
        parent = _sample_recipe()
        parent.design_id = "ash-coaster"
        parent.prompt = "embossed portrait"
        parent.generation_provider = "openscad"
        parent.provenance = {"tools_used": ["rembg"]}
        parent.stl_path = "/tmp/v1.stl"
        parent_path = save_recipe(parent, str(tmp_path))
        new = create_new_version(parent, parent_path, changes={"body.color": "white -> grey"})
        # Provenance lineage fields carried forward
        assert new.design_id == "ash-coaster"
        assert new.prompt == "embossed portrait"
        assert new.generation_provider == "openscad"
        assert new.provenance == {"tools_used": ["rembg"]}
        # stl_path invalidated (output changes per version)
        assert new.stl_path is None

    def test_create_new_version_deep_copies_provenance(self, tmp_path):
        """Mutating new.provenance must not affect parent.provenance."""
        parent = _sample_recipe()
        parent.provenance = {"tools_used": ["rembg"]}
        parent_path = save_recipe(parent, str(tmp_path))
        new = create_new_version(parent, parent_path)
        new.provenance["tools_used"].append("openscad")
        assert parent.provenance["tools_used"] == ["rembg"]


class TestListRecipeVersions:
    """list_recipe_versions: enumerate version history from disk."""

    def test_lists_all_versions(self, tmp_path):
        v1 = _sample_recipe()
        p1 = save_recipe(v1, str(tmp_path))
        v2 = create_new_version(v1, p1, changes={"body.color": "white -> red"})
        save_recipe(v2, str(tmp_path))
        versions = list_recipe_versions(str(tmp_path))
        assert len(versions) == 2
        assert versions[0]["version"] == 1
        assert versions[1]["version"] == 2

    def test_sorted_by_version(self, tmp_path):
        v1 = _sample_recipe()
        p1 = save_recipe(v1, str(tmp_path))
        v2 = create_new_version(v1, p1, notes="v2")
        p2 = save_recipe(v2, str(tmp_path))
        v3 = create_new_version(v2, p2, notes="v3")
        save_recipe(v3, str(tmp_path))
        versions = list_recipe_versions(str(tmp_path))
        assert [v["version"] for v in versions] == [1, 2, 3]

    def test_empty_directory(self, tmp_path):
        assert list_recipe_versions(str(tmp_path)) == []

    def test_nonexistent_directory(self):
        assert list_recipe_versions("/nonexistent") == []

    def test_includes_changes_and_notes(self, tmp_path):
        v1 = _sample_recipe()
        p1 = save_recipe(v1, str(tmp_path))
        v2 = create_new_version(
            v1, p1,
            changes={"body.color": "white -> red"},
            notes="recolor body",
        )
        save_recipe(v2, str(tmp_path))
        versions = list_recipe_versions(str(tmp_path))
        v2_entry = versions[1]
        assert v2_entry["notes"] == "recolor body"
        assert v2_entry["changes"] == {"body.color": "white -> red"}


def test_from_dict_tolerates_slot_seed_created_at():
    """A recipe seeded by the design-slot primitive carries ``created_at``
    (not ``created``).  Loading it must not raise — otherwise the first
    save on a freshly-created design slot crashes."""
    seed = {
        "design_id": "demo",
        "name": "Demo",
        "description": "seeded slot",
        "created_at": "2026-06-17T00:00:00+00:00",
        "archived": False,
    }
    recipe = DesignRecipe.from_dict(seed)
    assert recipe.name == "Demo"
    assert recipe.created == "2026-06-17T00:00:00+00:00"


def test_from_dict_prefers_created_over_created_at():
    """When both fields are present, ``created`` wins; absence of both
    degrades to an empty string rather than a KeyError."""
    both = {"name": "X", "created": "A", "created_at": "B"}
    assert DesignRecipe.from_dict(both).created == "A"
    neither = {"name": "X"}
    assert DesignRecipe.from_dict(neither).created == ""
